"""Переписка клиент ↔ команда через бота.

Клиент пишет (поддержка, вопрос по оплате или по заказу, обжалование бана) — у всех админов появляется
уведомление: заголовок с текстом и кнопками «✉️ Ответить» / «✅ Закрыть», вложения — копией ниже.
Админ отвечает кнопкой или просто свайпом-ответом на уведомление. Клиенту ответ приходит от имени бота
с кнопкой «💬 Ответить», и он тоже может ответить свайпом — всё склеивается в одно обращение.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb, SupCb
from app.bot.keyboards import cb, ik
from app.bot.notify import mark_headers, notify_admins, remember, remember_all, staff_targets
from app.config import Config
from app.db.models import Order, Ticket, User
from app.services import tickets
from app.services.users import display_name, short_name
from app.utils.timeutil import fmt_dt, now_utc, to_local

log = logging.getLogger(__name__)

CAPTIONED = {"photo", "document", "video", "audio", "voice", "animation"}
MEDIA_LABEL = {"photo": "📷 Фото", "document": "📎 Файл", "video": "🎬 Видео", "voice": "🎤 Голосовое",
               "audio": "🎵 Аудио", "sticker": "Стикер", "video_note": "🎥 Кружок", "animation": "GIF"}
MAX_TEXT = 3500


def content_type(m: Message) -> str:
    ct = m.content_type
    return str(getattr(ct, "value", ct))


def message_text(m: Message) -> str | None:
    return m.text if m.text is not None else m.caption


def message_html(m: Message) -> str:
    """Текст/подпись с форматированием отправителя (жирный, ссылки) — для пересылки от имени бота."""
    try:
        if m.text is not None:
            return m.html_text
        if m.caption is not None:
            return m.html_text
    except Exception:  # noqa: BLE001
        pass
    return html.escape(message_text(m) or "")


def copyable(m: Message) -> bool:
    return content_type(m) in {"text", *MEDIA_LABEL}


# ------------------------------------------------------------------ клиент → админы
def staff_kb(ticket: Ticket, level: int, *, banned: bool = False) -> InlineKeyboardMarkup:
    rows = [[cb("✉️ Ответить", AdminCb(s="t", a="reply", id=ticket.id, v="n")),
             cb("✅ Закрыть", AdminCb(s="t", a="close", id=ticket.id, v="n"))]]
    second = [cb("👤 Клиент", AdminCb(s="u", a="open", id=ticket.user_id))]
    if ticket.order_id:
        second.append(cb(f"📦 Заказ #{ticket.order_id}", AdminCb(s="o", a="open", id=ticket.order_id)))
    rows.append(second)
    if ticket.kind == "appeal" and banned and level >= 2:
        rows.append([cb("✅ Разбанить", AdminCb(s="t", a="unban", id=ticket.id, v="n"))])
    return ik(*rows)


def answered_kb(ticket: Ticket, level: int, *, banned: bool = False) -> InlineKeyboardMarkup:
    kb = staff_kb(ticket, level, banned=banned)
    kb.inline_keyboard[0][0] = cb("✉️ Ответить ещё", AdminCb(s="t", a="reply", id=ticket.id, v="n"))
    return kb


def closed_kb(ticket: Ticket, level: int) -> InlineKeyboardMarkup:
    return ik([cb("✉️ Написать", AdminCb(s="t", a="reply", id=ticket.id, v="n")),
               cb("👤 Клиент", AdminCb(s="u", a="open", id=ticket.user_id))])


@dataclass
class Item:
    """Сообщение клиента для уведомления админам (когда исходного Message уже нет — например, из БД)."""
    chat_id: int
    message_id: int
    content_type: str
    text: str | None = None


def item_of(m: Message) -> Item:
    return Item(m.chat.id, m.message_id, content_type(m), message_text(m))


async def staff_header(session: AsyncSession, ticket: Ticket, user: User, m: Message | None,
                       new: bool, *, items: list[Item] | None = None, note: str | None = None) -> str:
    lines = [f"{tickets.title(ticket)} · №{ticket.id}" + ("" if new else " · продолжение"),
             f"От: {display_name(user)}"]
    if ticket.kind == "appeal":
        lines.append("🚫 Забанен" + (f": {html.escape(user.ban_reason)}" if user.ban_reason else "")
                     + (f" · {fmt_dt(user.banned_at)}" if user.banned_at else ""))
    if ticket.order_id:
        order = await session.get(Order, ticket.order_id)
        if order is not None:
            lines.append(f"Заказ #{order.id}: {texts.SUBJECTS.get(order.subject or '', '—')} · "
                         f"{texts.STATUS.get(order.status, order.status)}")
    if note:
        lines.append(note)
    parts = items if items is not None else ([item_of(m)] if m is not None else [])
    media: dict[str, int] = {}
    for it in parts:
        if it.content_type != "text":
            media[it.content_type] = media.get(it.content_type, 0) + 1
    for ct, n in media.items():
        lines.append(f"{MEDIA_LABEL.get(ct, ct)}" + (f" ×{n}" if n > 1 else "") + " — ниже ⬇️")
    body = "\n\n".join(it.text for it in parts if it.text)
    if body:
        cut = body if len(body) <= MAX_TEXT else body[:MAX_TEXT] + "…"
        lines.append(f"<blockquote>{html.escape(cut)}</blockquote>")
    lines.append(texts.ADMIN_REPLY_HINT.strip())
    return "\n".join(lines)


async def to_staff(bot: Bot, session: AsyncSession, config: Config, ticket: Ticket, user: User, m: Message,
                   *, new: bool, header: bool = True, min_level: int = 1) -> int:
    """Уведомление админам о сообщении клиента. header=False — только копия вложения (2-е фото альбома).
    Без commit (запоминает сообщения в admin_messages). Возвращает, скольким админам дошло."""
    return await items_to_staff(bot, session, config, ticket, user, [item_of(m)], new=new, header=header,
                                min_level=min_level)


async def items_to_staff(bot: Bot, session: AsyncSession, config: Config, ticket: Ticket, user: User,
                         items: list[Item], *, new: bool, header: bool = True, min_level: int = 1,
                         note: str | None = None) -> int:
    """То же для нескольких сообщений сразу: один заголовок, вложения — копиями ниже. Без commit."""
    delivered = 0
    if header:
        text = await staff_header(session, ticket, user, None, new, items=items, note=note)
        sent = await notify_admins(bot, config, text, min_level=min_level,
                                   markup_for=lambda lvl: staff_kb(ticket, lvl, banned=user.is_banned))
        remember_all(session, sent, "ticket", ticket.id, user.id, text)
        delivered = len(sent)
    media = [it for it in items if it.content_type != "text"]
    if media:
        for admin_id, _lvl in staff_targets(config, min_level):
            for it in media:
                try:
                    copy = await bot.copy_message(admin_id, it.chat_id, it.message_id)
                    remember(session, admin_id, copy.message_id, "ticket", ticket.id, user.id)
                    if not header:
                        delivered += 1
                except (TelegramBadRequest, TelegramForbiddenError) as e:
                    log.warning("копия сообщения клиента админу %s не ушла: %s", admin_id, e)
    return delivered


# ------------------------------------------------------------------ админ → клиент
def user_reply_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return ik([cb(texts.SUPPORT_REPLY_BTN, SupCb(action="reply", id=ticket_id))])


def team_header(ticket: Ticket, order: Order | None = None) -> str:
    if order is not None and order.subject == "explain":
        return texts.EXPLAIN_REPLY_HEADER.format(id=order.id)
    if ticket.kind == "order" and ticket.order_id:
        return f"💬 <b>Сообщение по заказу #{ticket.order_id}</b>"
    return texts.SUPPORT_FROM_TEAM


@dataclass
class Sent:
    """Итог доставки клиенту: id сообщений в его чате (по одному на присланное админом), отдельный заголовок
    (у альбома и стикера), либо «заблокировал бота» / текст ошибки Telegram."""
    message_ids: list[int] = field(default_factory=list)
    header_id: int | None = None
    blocked: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.message_ids)

    @property
    def message_id(self) -> int | None:  # совместимость: «доставленное сообщение»
        return self.message_ids[0] if self.message_ids else None


def _mid(value) -> int:  # noqa: ANN001
    return int(getattr(value, "message_id", value))


async def to_user(bot: Bot, ticket: Ticket, m: Message, header: str) -> Sent:
    """Доставляет сообщение админа клиенту от имени бота с пометкой header ("" — без пометки).
    Текст — новым сообщением, фото/файлы/видео/голосовые — копией с подписью, остальное (стикер, кружок,
    геолокация…) — пометкой и копией. Под сообщением — кнопка «💬 Ответить»."""
    kb = user_reply_kb(ticket.id)
    ct = content_type(m)
    sent = Sent()
    try:
        if ct == "text":
            body = message_html(m)
            text = f"{header}\n\n{body}" if header else body
            if len(text) <= 4096:
                msg = await bot.send_message(ticket.user_id, text, reply_markup=kb)
                sent.message_ids.append(msg.message_id)
                return sent
        elif ct in CAPTIONED:
            body = message_html(m) if m.caption else ""
            caption = "\n\n".join(x for x in (header, body) if x)
            if len(caption) <= 1024:
                mid = await bot.copy_message(ticket.user_id, m.chat.id, m.message_id, caption=caption or None,
                                             reply_markup=kb)
                sent.message_ids.append(_mid(mid))
                return sent
        if header:
            sent.header_id = (await bot.send_message(ticket.user_id, header)).message_id
        mid = await bot.copy_message(ticket.user_id, m.chat.id, m.message_id, reply_markup=kb)
        sent.message_ids.append(_mid(mid))
    except TelegramForbiddenError:
        sent.message_ids.clear()
        sent.blocked = True
    except TelegramBadRequest as e:
        log.warning("ответ клиенту %s не доставлен: %s", ticket.user_id, e)
        sent.message_ids.clear()
        sent.error = e.message
    return sent


async def album_to_user(bot: Bot, ticket: Ticket, msgs: list[Message], header: str) -> Sent:
    """Альбом админа (до 10 фото/видео/файлов) — целиком, одним альбомом через copyMessages; пометка и кнопка
    «💬 Ответить» — сообщением над ним (у альбома в Telegram не бывает кнопок)."""
    sent = Sent()
    try:
        head = await bot.send_message(ticket.user_id, header or texts.SUPPORT_FROM_TEAM,
                                      reply_markup=user_reply_kb(ticket.id))
        sent.header_id = head.message_id
        ids = await bot.copy_messages(ticket.user_id, msgs[0].chat.id, [m.message_id for m in msgs])
        sent.message_ids = [_mid(x) for x in ids]
    except TelegramForbiddenError:
        sent.message_ids.clear()
        sent.blocked = True
    except TelegramBadRequest as e:
        log.warning("альбом клиенту %s не доставлен: %s", ticket.user_id, e)
        sent.message_ids.clear()
        sent.error = e.message
    return sent


async def reply_to_ticket(bot: Bot, session: AsyncSession, config: Config, ticket: Ticket, admin: User | None,
                          admin_id: int, m: Message, order: Order | None = None, header: str | None = None) -> bool:
    """Ответ админа: доставить, записать в обращение, пометить уведомления у остальных админов. Коммитит."""
    if not copyable(m):
        return False
    if order is None and ticket.order_id:
        order = await session.get(Order, ticket.order_id)
    delivered = await to_user(bot, ticket, m, team_header(ticket, order) if header is None else header)
    if not delivered.ok:
        return False
    now = now_utc()
    tickets.add_out(session, ticket, admin_id, content_type(m), message_text(m), delivered.message_id, now)
    await session.commit()
    await mark_answered(bot, session, config, ticket, admin, admin_id, now)
    return True


async def mark_answered(bot: Bot, session: AsyncSession, config: Config, ticket: Ticket, admin: User | None,
                        admin_id: int, now: datetime) -> None:
    """У всех админов на уведомлениях об обращении — «✅ Ответил(а): Аня · 14:05»."""
    user = await session.get(User, ticket.user_id)
    marker = f"✅ Ответил(а): {short_name(admin) if admin else admin_id} · {to_local(now):%H:%M}"
    await mark_headers(bot, session, config, "ticket", ticket.id, marker,
                       markup_for=lambda lvl: answered_kb(ticket, lvl, banned=bool(user and user.is_banned)))
