"""Глобальный обработчик ошибок: лог + уведомление админам + вежливый ответ пользователю."""
from __future__ import annotations

import html
import logging
import time

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ErrorEvent

from app import actionlog, texts
from app.bot.notify import notify_admins
from app.config import Config

log = logging.getLogger(__name__)
router = Router(name="errors")

NOTIFY_EVERY = 60.0  # одну и ту же ошибку админам - не чаще раза в минуту
_last_sent: dict[str, float] = {}

HARMLESS = ("message is not modified", "query is too old", "query id is invalid",
            "message to edit not found", "message can't be edited")


def _user(event: ErrorEvent):  # noqa: ANN202
    u = event.update
    for obj in (u.callback_query, u.message, u.pre_checkout_query, u.edited_message):
        if obj is not None and getattr(obj, "from_user", None) is not None:
            return obj.from_user
    return None


def _where(event: ErrorEvent) -> str:
    """Где случилась ошибка. v2.4: без содержимого сообщения (там мог быть промокод), только тип и длина."""
    u = event.update
    try:
        what = actionlog.describe(u)
    except Exception:  # noqa: BLE001
        what = None
    if u.callback_query is not None:
        what = f"{what or 'кнопка'} [{(u.callback_query.data or '')[:40]}]"
    if u.pre_checkout_query is not None:
        what = "подтверждение оплаты"
    user = _user(event)
    return f"{what or f'update {u.update_id}'}, {actionlog.who(user)}" if user else (what or f"update {u.update_id}")


def _should_notify(key: str) -> bool:
    now = time.monotonic()
    if now - _last_sent.get(key, 0.0) < NOTIFY_EVERY:
        return False
    _last_sent[key] = now
    if len(_last_sent) > 1000:
        _last_sent.clear()
        _last_sent[key] = now
    return True


async def _answer_user(event: ErrorEvent, bot: Bot) -> None:
    u = event.update
    try:
        if u.callback_query is not None:
            await u.callback_query.answer(texts.ERROR_USER, show_alert=True)
        elif u.pre_checkout_query is not None:
            await u.pre_checkout_query.answer(ok=False, error_message=texts.ERROR_USER)
        elif u.message is not None and u.message.chat.type == "private":
            await bot.send_message(u.message.chat.id, texts.ERROR_USER)
    except Exception:  # noqa: BLE001 - пользователь мог заблокировать бота, кнопка могла устареть
        log.debug("не удалось ответить пользователю об ошибке", exc_info=True)


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot, config: Config) -> bool:
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and any(h in str(exc).lower() for h in HARMLESS):
        log.info("безобидная ошибка Telegram: %s", exc)
        return True
    log.error("ошибка при обработке: %s", _where(event), exc_info=(type(exc), exc, exc.__traceback__))
    actionlog.action(f"ошибка {type(exc).__name__}, бот ответил «Что-то пошло не так»", _user(event),
                     level=logging.ERROR)
    key = f"{type(exc).__name__}:{str(exc)[:120]}"
    if _should_notify(key):
        error = html.escape(f"{type(exc).__name__}: {exc}"[:600])
        await notify_admins(bot, config, texts.ADMIN_ERROR.format(error=error)
                            + f"\n<i>{html.escape(_where(event))}</i>\nПодробности: в data/logs/aegis.log",
                            min_level=2)
    await _answer_user(event, bot)
    return True
