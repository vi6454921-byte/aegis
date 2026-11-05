from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.bot import drafts
from app.bot.keyboards import main_menu, set_menu_admin, set_menu_draft
from app.bot.pinned import on_update_user
from app.config import Config
from app.services import pinned, roles
from app.services.settings import load_settings
from app.services.users import register_user
from app.utils.timeutil import now_utc

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
log = logging.getLogger(__name__)


class AntiFloodMiddleware(BaseMiddleware):
    """Корзина токенов на пользователя: до `burst` событий подряд, восполняется `rate` в секунду.

    Альбом (до 10 фото с одним media_group_id) стоит как одно событие: раньше часть фото большого альбома
    отсекалась антифлудом и терялась."""

    ALBUM_TTL = 120.0

    def __init__(self, config: Config, rate: float = 1.5, burst: int = 20):
        self.config, self.rate, self.burst = config, rate, burst
        self.buckets: dict[int, tuple[float, float]] = {}
        self.albums: dict[tuple[int, str], float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if not isinstance(event, Update) or user is None or self.config.admin_level(user.id) > 0 \
                or event.pre_checkout_query is not None or event.chat_member is not None \
                or event.my_chat_member is not None \
                or (event.message is not None and event.message.successful_payment is not None):
            return await handler(event, data)
        now = time.monotonic()
        album = event.message.media_group_id if event.message is not None else None
        if album:
            key = (user.id, album)
            seen = self.albums.get(key)
            if seen is not None and now - seen < self.ALBUM_TTL:
                return await handler(event, data)  # следующее фото того же альбома — бесплатно
            self.albums[key] = now
            if len(self.albums) > 10000:
                self.albums = {k: t for k, t in self.albums.items() if now - t < self.ALBUM_TTL}
        tokens, last = self.buckets.get(user.id, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self.buckets[user.id] = (tokens, now)
            if event.callback_query is not None:
                await event.callback_query.answer(texts.FLOOD)
            return None
        self.buckets[user.id] = (tokens - 1, now)
        if len(self.buckets) > 50000:
            self.buckets.clear()
        return await handler(event, data)


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            return await handler(event, data)


def _chat_id(event: Update) -> int | None:
    """Чат пользователя для сообщений и нажатий кнопок (остальные события — None)."""
    if event.message is not None:
        return event.message.chat.id
    if event.callback_query is not None and event.callback_query.message is not None:
        return event.callback_query.message.chat.id
    return None


def _private(event: Update) -> bool | None:
    """True — личный чат, False — группа/канал, None — у события нет чата (pre_checkout, inline)."""
    chat = None
    if event.message is not None:
        chat = event.message.chat
    elif event.callback_query is not None and event.callback_query.message is not None:
        chat = event.callback_query.message.chat
    elif event.my_chat_member is not None:
        chat = event.my_chat_member.chat
    elif event.chat_member is not None:
        chat = event.chat_member.chat
    elif event.edited_message is not None:
        chat = event.edited_message.chat
    return None if chat is None else chat.type == "private"


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя (реферал и метка — только при первом /start), грузит настройки и роли.

    В data: s (настройки), admin_level (0 — клиент, 1, 2), is_admin, is_owner, db_user, is_new_user, referrer,
    is_banned (админы забаненными не бывают). Сам бан обрабатывает роутер handlers/banned.py.

    v2.2: человек с ником из SENIOR_ADMIN_USERNAMES впервые пишет боту — привязываем его id (закреплённый админ
    2 уровня, app/services/pinned.py). Ожидание скрина по заданию (PromoSG) тихо отменяется, если человек нажал
    кнопку другого раздела: задания не мешают меню и заказам."""

    def __init__(self, config: Config):
        self.config = config

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        session: AsyncSession = data["session"]
        await roles.ensure_loaded(session, self.config)
        data["s"] = await load_settings(session)
        tg_user = data.get("event_from_user")
        level = self.config.admin_level(tg_user.id) if tg_user else 0
        data["admin_level"] = level
        data["is_admin"] = level > 0
        data["is_owner"] = bool(tg_user and tg_user.id in self.config.admin_ids)
        data["is_banned"] = False
        set_menu_admin(level > 0)
        if not isinstance(event, Update):
            return await handler(event, data)
        private = _private(event)
        if event.chat_member is not None or (event.my_chat_member is not None and private is False):
            return await handler(event, data)  # события канала: регистрировать некого
        if private is False:
            return None  # бота добавили в группу — там он не работает
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)
        payload = None
        msg = event.message
        if msg is not None and msg.text and msg.text.startswith("/start"):
            parts = msg.text.split(maxsplit=1)
            payload = parts[1] if len(parts) > 1 else None
        reg = await register_user(session, tg_user, payload, now_utc())
        if private and pinned.wants(self.config, tg_user):
            await on_update_user(data["bot"], session, self.config, tg_user)
            level = self.config.admin_level(tg_user.id)
            data["admin_level"], data["is_admin"] = level, level > 0
            data["is_owner"] = tg_user.id in self.config.admin_ids
            set_menu_admin(level > 0)
        cq = event.callback_query
        if cq is not None and str(data.get("raw_state") or "").startswith("PromoSG:") \
                and not (cq.data or "").startswith("pr:") and data.get("state") is not None:
            await data["state"].clear()  # ушёл в другой раздел — ожидание скрина тихо отменяется
            data["raw_state"] = None
        data["db_user"] = reg.user
        data["is_new_user"] = reg.created
        data["referrer"] = reg.referrer
        data["is_banned"] = bool(reg.user.is_banned and level == 0)
        if data["is_banned"] and event.pre_checkout_query is not None:
            await event.pre_checkout_query.answer(ok=False, error_message=texts.BANNED_SHORT)
            return None
        set_menu_draft(None)
        drafts.mark_shown(False)
        chat_id = _chat_id(event)
        if private and chat_id is not None and not data["is_banned"]:
            await drafts.refresh_menu(session, tg_user.id)
        before = str(data.get("raw_state") or "")
        result = await handler(event, data)
        if before.startswith("OrderSG:") and chat_id is not None and not drafts.was_shown() \
                and data.get("state") is not None:
            await self._draft_saved_notice(data, session, tg_user.id, chat_id)
        return result

    async def _draft_saved_notice(self, data: dict[str, Any], session: AsyncSession, user_id: int,
                                  chat_id: int) -> None:
        """Ушёл из оформления (кнопка меню, команда) — говорим, что черновик цел, и даём кнопку вернуться."""
        now_state = str(await data["state"].get_state() or "")
        if now_state.startswith("OrderSG:"):
            return
        try:
            info = await drafts.refresh_menu(session, user_id)
            if info is not None:
                await data["bot"].send_message(chat_id, texts.DRAFT_SAVED.format(n=info.photos),
                                               reply_markup=main_menu())
        except Exception:  # noqa: BLE001 — подсказка не должна ломать основной ответ
            log.warning("не удалось показать подсказку о черновике %s", user_id, exc_info=True)
