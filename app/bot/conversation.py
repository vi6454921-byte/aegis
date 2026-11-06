"""v2.3: переписка админа с клиентом по заказу (и по обращению) - режим чата и доставка.

«✉️ Ответить» в карточке заказа, в карточке обращения или под уведомлением о новом заказе включает режим чата:
строка в admin_chats (переживает перезапуск бота). Всё, что админ присылает боту - текст, фото, альбомы, файлы
(HTML, ZIP…), голосовые, видео, стикеры, - сразу уходит клиенту с пометкой «По заказу №…», а под сообщением
админа появляется ответ «✅ Доставлено» (или понятная ошибка: клиент заблокировал бота и т. п.).

Режим действует, пока админ не нажмёт «✅ Завершить» (или «📦 Готово - отправить клиенту»). Другие кнопки
и вводы админки его не ломают: пока админ вводит цену или ищет пользователя, сообщение идёт туда, а следующее - снова клиенту. В /admin есть «↩️ Вернуться в чат по заказу №…».

Ответ админа свайпом на уведомление о заказе доставляется так же. Клиент отвечает свайпом (из любого шага) или
кнопкой «💬 Ответить» - всё складывается в одну переписку по заказу (tickets kind=order, ticket_messages):
кто, что и когда - видно в карточке заказа («💬 Переписка»)."""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.bot import designs
from app.bot import files as upl
from app.bot.callbacks import AdminCb
from app.bot.keyboards import cb, ik, order_alert_kb
from app.bot.notify import mark_headers
from app.bot.support import Sent, album_to_user, content_type, mark_answered, message_text, team_header, to_user
from app.config import Config
from app.db.models import AdminChat, Order, Ticket, TicketMessage, User
from app.services import design as dsvc
from app.services import orders as svc
from app.services import tickets
from app.services.users import short_name
from app.utils.timeutil import fmt_dt, now_utc, to_local

log = logging.getLogger(__name__)
SERVICE_TYPES = {"new_chat_members", "left_chat_member", "pinned_message", "successful_payment",
                 "web_app_data", "unknown"}


@dataclass(frozen=True)
class Target:
    """Кому и по какому поводу пишет админ: заказ (order_id) или обращение без заказа (ticket_id)."""
    user_id: int
    order_id: int | None = None
    ticket_id: int | None = None

    @classmethod
    def of(cls, chat: AdminChat) -> Target:
        return cls(chat.user_id, chat.order_id, chat.ticket_id)

    def same(self, chat: AdminChat | None) -> bool:
        if chat is None:
            return False
        if self.order_id:
            return chat.order_id == self.order_id
        return chat.order_id is None and chat.ticket_id == self.ticket_id

    @property
    def mark(self) -> str:
        """Пометка для админа: «По заказу №5» / «По обращению №12»."""
        return texts.CHAT_MARK_ORDER.format(id=self.order_id) if self.order_id \
            else texts.CHAT_MARK_TICKET.format(id=self.ticket_id)


def user_label(user: User | None) -> str:
    """«@username», а без ника - «Имя (id 123)». Экранировано для HTML."""
    if user is None:
        return "-"
    if user.username:
        return f"@{html.escape(user.username)}"
    return f"{html.escape(user.first_name or texts.CHAT_NO_NAME)} (id <code>{user.id}</code>)"


def user_header(target: Target, ticket: Ticket) -> str:
    """Пометка для клиента над сообщением админа."""
    if target.order_id:
        return texts.CHAT_TO_USER.format(mark=texts.CHAT_MARK_ORDER.format(id=target.order_id))
    return team_header(ticket)


# ------------------------------------------------------------------ режим чата (admin_chats)
async def get_chat(session: AsyncSession, admin_id: int) -> AdminChat | None:
    return await session.get(AdminChat, admin_id, populate_existing=True)


async def start(session: AsyncSession, admin_id: int, target: Target) -> str | None:
    """Включает режим чата. Возвращает пометку прежнего чата, если админ переключился с другого клиента/заказа."""
    now = now_utc()
    chat = await get_chat(session, admin_id)
    previous = None
    if chat is None:
        session.add(AdminChat(admin_id=admin_id, user_id=target.user_id, order_id=target.order_id,
                              ticket_id=target.ticket_id, started_at=now, sent=0))
    elif target.same(chat):
        chat.user_id = target.user_id
        chat.ticket_id = target.ticket_id or chat.ticket_id
    else:
        previous = Target.of(chat).mark
        chat.user_id, chat.order_id, chat.ticket_id = target.user_id, target.order_id, target.ticket_id
        chat.started_at, chat.last_out_at, chat.sent = now, None, 0
    await session.commit()
    return previous


async def finish(session: AsyncSession, admin_id: int, target: Target | None = None) -> AdminChat | None:
    """Выключает режим чата (если target задан - только если чат именно про него). Возвращает снятый чат."""
    chat = await get_chat(session, admin_id)
    if chat is None or (target is not None and not target.same(chat)):
        return None
    await session.delete(chat)
    await session.commit()
    return chat


async def finish_ticket(session: AsyncSession, ticket_id: int) -> list[AdminChat]:
    """Обращение (без заказа) закрыли - режим чата по нему выключаем у всех админов. Коммитит."""
    rows = list((await session.execute(select(AdminChat).where(AdminChat.ticket_id == ticket_id,
                                                               AdminChat.order_id.is_(None)))).scalars().all())
    for chat in rows:
        await session.delete(chat)
    if rows:
        await session.commit()
    return rows


async def thread_ticket(session: AsyncSession, target: Target) -> Ticket | None:
    """Обращение, в которое пишем: по заказу - единая ветка заказа (создаём при первом сообщении)."""
    if target.order_id:
        ticket, _ = await tickets.open_or_append(session, target.user_id, "order", now_utc(), target.order_id)
        return ticket
    return await tickets.get(session, target.ticket_id) if target.ticket_id else None


def chat_kb(target: Target, order: Order | None) -> InlineKeyboardMarkup:
    """Под заголовком чата и под каждым «✅ Доставлено»: «📦 Готово - отправить клиенту» (если можно) и «✅ Завершить»."""
    rows = []
    if order is not None and order.status in ("paid", "in_work") and (svc.is_explain(order) or svc.can_finish(order)):
        rows.append([cb(texts.CHAT_READY_BTN, AdminCb(s="c", a="done", id=order.id))])
    rows.append([cb(texts.CHAT_FINISH_BTN, AdminCb(s="c", a="end", id=target.order_id or target.ticket_id or 0,
                                                   v="o" if target.order_id else "t"))])
    return ik(*rows)


def return_btn(chat: AdminChat):  # noqa: ANN201
    label = texts.CHAT_RETURN_BTN.format(id=chat.order_id) if chat.order_id \
        else texts.CHAT_RETURN_TICKET_BTN.format(id=chat.ticket_id)
    return cb(label, AdminCb(s="c", a="back"))


# ------------------------------------------------------------------ история
async def history_lines(session: AsyncSession, rows: list[TicketMessage], width: int = 200) -> list[str]:
    """«👤 Клиент · 12.05 14:03 - текст» / «🛡 Аня · 12.05 14:05 - [📷 фото] подпись»."""
    names: dict[int | None, str] = {}
    out = []
    for m in rows:
        if m.content_type == "album":
            continue
        body = tickets.preview_of(m.text, m.content_type)[:width] or "-"
        if m.direction == "in":
            who = texts.CHAT_FROM_CLIENT
        else:
            if m.admin_id not in names:
                names[m.admin_id] = short_name(await session.get(User, m.admin_id)) if m.admin_id \
                    else texts.CHAT_BOT_NAME
            who = texts.CHAT_FROM_ADMIN.format(name=names[m.admin_id])
        out.append(f"{who} · {fmt_dt(m.created_at)}: {html.escape(body)}")
    return out


async def header_view(session: AsyncSession, chat: AdminChat, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Заголовок режима чата: «✉️ Вы пишете клиенту @username по заказу №5», подсказка, последние сообщения."""
    target = Target.of(chat)
    user = await session.get(User, chat.user_id)
    order = await svc.get_order(session, chat.order_id) if chat.order_id else None
    if chat.order_id:
        title = texts.CHAT_HEADER_ORDER.format(user=user_label(user), id=chat.order_id)
        mark = texts.CHAT_MARK_ORDER.format(id=chat.order_id)
        rows, total = await tickets.order_thread(session, chat.order_id, 5)
    else:
        title = texts.CHAT_HEADER_TICKET.format(user=user_label(user), id=chat.ticket_id)
        mark = texts.CHAT_MARK_SUPPORT
        rows = [m for m in await tickets.messages(session, chat.ticket_id or 0, 6) if m.content_type != "album"][-5:]
        total = len(rows)
    lines = ([note, ""] if note else []) + [title, texts.CHAT_HEADER_HINT.format(mark=mark)]
    if rows:
        lines.append(texts.CHAT_CARD_TITLE.format(n=len(rows), total=total))
        lines += await history_lines(session, rows, 160)
    kb = chat_kb(target, order)
    extra = [cb(texts.CHAT_ORDER_BTN.format(id=chat.order_id), AdminCb(s="o", a="open", id=chat.order_id))] \
        if chat.order_id else \
        [cb(texts.CHAT_TICKET_BTN.format(id=chat.ticket_id), AdminCb(s="t", a="open", id=chat.ticket_id or 0))]
    kb.inline_keyboard.append(extra)
    text = "\n".join(lines)
    return (text if len(text) <= 4000 else text[:3990] + "…"), kb


# ------------------------------------------------------------------ доставка
async def send(bot: Bot, session: AsyncSession, config: Config, admin_id: int, target: Target,
               msgs: list[Message]) -> tuple[Sent, Order | None]:
    """Доставить клиенту сообщение админа (или альбом целиком) и записать в переписку. Коммитит."""
    now = now_utc()
    order = await svc.get_order(session, target.order_id) if target.order_id else None
    if target.order_id and order is None:
        return Sent(error=texts.CHAT_CLOSED_FOR_ADMIN.format(id=target.order_id)), None
    ticket = await thread_ticket(session, target)
    if ticket is None:
        return Sent(error=texts.NOT_FOUND), order
    design = dsvc.is_design(order)
    if order is not None and (svc.is_explain(order) or design) and order.status == "paid":
        order = await svc.take_in_work(session, order.id, now, admin_id)  # начал объяснять/делать - взял в работу
        admin = await session.get(User, admin_id)
        mark = texts.DESIGN_WORKING if design else texts.CHAT_EXPLAINING
        await mark_headers(bot, session, config, "order", order.id,
                           mark.format(admin=short_name(admin), time=f"{to_local(now):%H:%M}"),
                           markup_for=lambda _lvl, oid=order.id: designs.admin_kb(oid) if design else order_alert_kb(oid))
    was_open = ticket.status == "open"
    header = user_header(target, ticket)
    sent = await (to_user(bot, ticket, msgs[0], header) if len(msgs) == 1
                  else album_to_user(bot, ticket, msgs, header))
    if sent.ok:
        if len(msgs) > 1 and sent.header_id:
            tickets.add_out(session, ticket, admin_id, "album", None, sent.header_id, now)
        for m, mid in zip(msgs, sent.message_ids):
            tickets.add_out(session, ticket, admin_id, content_type(m), message_text(m), mid, now)
        chat = await get_chat(session, admin_id)
        if target.same(chat):
            chat.sent += len(msgs)
            chat.last_out_at = now
        await session.commit()
        if was_open:
            await mark_answered(bot, session, config, ticket, await session.get(User, admin_id), admin_id, now)
    if order is not None:
        order = await svc.note_chat_reply(session, order.id, admin_id, sent.ok) or order
    if order is not None and design and sent.ok and designs.is_result([content_type(m) for m in msgs]):
        order = await designs.on_result(bot, session, config, admin_id, order)  # v2.4: готовый файл - заказ готов
    return sent, order


def confirm_text(sent: Sent, n: int) -> str:
    if sent.ok:
        return texts.CHAT_DELIVERED + (texts.CHAT_DELIVERED_ALBUM.format(n=n) if n > 1 else "")
    if sent.blocked:
        return texts.CHAT_BLOCKED
    return texts.CHAT_FAILED.format(error=html.escape(sent.error or texts.CHAT_NOT_ACCEPTED))


async def relay(bot: Bot, session: AsyncSession, config: Config, admin_id: int, target: Target,
                msgs: list[Message], *, chat_mode: bool) -> Sent:
    """Доставить и ответить админу на его сообщение: «✅ Доставлено» / ошибка.
    chat_mode - под ответом кнопки «✅ Завершить» (и «📦 Готово»), иначе (свайп) - «📨 Переписка»."""
    if any(content_type(m) in SERVICE_TYPES for m in msgs):
        return Sent(error=texts.CHAT_SERVICE_MESSAGE)
    sent, order = await send(bot, session, config, admin_id, target, msgs)
    text = confirm_text(sent, len(msgs))
    if sent.ok and dsvc.is_design(order) and order.status == "done" \
            and designs.is_result([content_type(m) for m in msgs]):
        text += "\n" + texts.ADMIN_DESIGN_DONE_NOTE.format(id=order.id)
    if chat_mode:
        kb = chat_kb(target, order)
    elif target.order_id:
        kb = ik([cb(texts.CHAT_HISTORY_BTN, AdminCb(s="o", a="chat", id=target.order_id, v="n")),
                 cb(texts.CHAT_BTN, AdminCb(s="c", a="o", id=target.order_id))])
    else:
        kb = ik([cb(texts.CHAT_TICKET_OPEN_BTN, AdminCb(s="t", a="open", id=target.ticket_id or 0))])
    if not chat_mode and sent.ok:
        text += f" · {target.mark}"
    try:
        await msgs[0].reply(text, reply_markup=kb)
    except Exception:  # noqa: BLE001 - подтверждение не должно ломать доставку
        log.warning("не удалось подтвердить доставку админу %s", admin_id, exc_info=True)
    return sent


# ------------------------------------------------------------------ альбомы: собираем по media_group_id
_albums: dict[tuple[int, str], tuple[Target, bool, list[Message]]] = {}


async def relay_or_buffer(bot: Bot, session: AsyncSession, sessionmaker: async_sessionmaker, config: Config,
                          message: Message, target: Target, *, chat_mode: bool) -> None:
    """Обычное сообщение - сразу; альбом - копим (Telegram присылает его по одному фото) и отправляем целиком."""
    admin_id = message.from_user.id
    album = message.media_group_id
    if not album:
        await relay(bot, session, config, admin_id, target, [message], chat_mode=chat_mode)
        return
    key = (admin_id, album)
    entry = _albums.get(key)
    if entry is None:
        entry = _albums[key] = (target, chat_mode, [])
    entry[2].append(message)

    async def flush() -> None:
        tgt, mode, msgs = _albums.pop(key, (target, chat_mode, []))
        if not msgs:
            return
        msgs.sort(key=lambda m: m.message_id)
        async with sessionmaker() as s2:
            for i in range(0, len(msgs), 10):
                await relay(bot, s2, config, admin_id, tgt, msgs[i:i + 10], chat_mode=mode)

    upl.schedule_ack(admin_id, f"chat:{album}", flush)
