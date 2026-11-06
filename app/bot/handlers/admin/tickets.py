"""Админка → обращения: входящие, переписка, ответ кнопкой или свайпом, закрытие, разбан по обжалованию.

v2.3: «✉️ Ответить» включает режим чата (admin/chat.py); свайп-ответ на уведомление доставляется тем же
механизмом (app.bot.conversation): по заказу - в единую переписку заказа, с подтверждением «✅ Доставлено»."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.bot import conversation
from app.bot.callbacks import AdminCb
from app.bot.conversation import Target
from app.bot.handlers.admin.base import admin_router, menu_btn
from app.bot.handlers.admin.chat import open_chat, ticket_target
from app.bot.keyboards import cb, ik, main_menu, pager
from app.bot.notify import lookup, mark_headers, safe_send
from app.bot.support import closed_kb
from app.bot.ui import show
from app.config import Config
from app.db.models import Order, Payment, Ticket, User
from app.services import orders as svc
from app.services import tickets
from app.services.admin_log import log_admin
from app.services.users import display_name, short_name
from app.utils.timeutil import fmt_dt, now_utc, to_local

router = admin_router("admin_tickets")
PER_PAGE = 8


def T(a: str = "", ticket_id: int = 0, v: str = "", p: int = 0) -> AdminCb:  # noqa: N802
    return AdminCb(s="t", a=a, id=ticket_id, v=v, p=p)


def _f(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "t") & (F.a == action))


# ------------------------------------------------------------------ экраны
async def inbox_view(session: AsyncSession, page: int, only_open: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows, total = await tickets.inbox(session, page, PER_PAGE, only_open)
    n_open = await tickets.open_count(session)
    lines = [f"📨 <b>Обращения</b>\nЖдут ответа: <b>{n_open}</b>",
             "<i>Сначала те, что ждут дольше всех.</i>" if only_open else "<i>Все незакрытые, свежие сверху.</i>"]
    buttons = []
    for t in rows:
        user = await session.get(User, t.user_id)
        icon = "🔴" if t.status == "open" else "✅"
        kind = tickets.KINDS.get(t.kind, "").split(" ")[0]
        preview = (t.preview or "").replace("\n", " ")
        buttons.append([cb(f"{icon} №{t.id} {kind} {short_name(user)} · {preview[:28]}", T("open", t.id))])
    if not rows:
        lines.append("\nПусто 👌")
    v = "o" if only_open else "a"
    return "\n".join(lines), ik(
        *buttons,
        pager(page, total, PER_PAGE, lambda p: T("list", v=v, p=p)),
        [cb(("• " if only_open else "") + "🔴 Ждут ответа", T("list", v="o")),
         cb(("• " if not only_open else "") + "🗂 Все открытые", T("list", v="a"))],
        [menu_btn()])


async def ticket_view(session: AsyncSession, ticket: Ticket, level: int) -> tuple[str, InlineKeyboardMarkup]:
    user = await session.get(User, ticket.user_id, populate_existing=True)
    lines = [f"{tickets.title(ticket)} · №{ticket.id} · {tickets.STATUS.get(ticket.status, ticket.status)}",
             f"Клиент: {display_name(user)}"]
    if ticket.kind == "appeal" and user is not None:
        lines.append("Статус: " + ("🚫 забанен" + (f": {html.escape(user.ban_reason)}" if user.ban_reason else "")
                                   if user.is_banned else "✅ уже разбанен"))
    if ticket.order_id:
        order = await session.get(Order, ticket.order_id)
        if order is not None:
            lines.append(f"Заказ #{order.id}: {texts.STATUS.get(order.status, order.status)}")
    lines.append(f"Создано {fmt_dt(ticket.created_at)} · обновлено {fmt_dt(ticket.updated_at)}")
    history = await tickets.messages(session, ticket.id, 12)
    has_media = False
    if history:
        lines.append("\n<b>Переписка</b> (последние сообщения):")
    names: dict[int, str] = {}
    for m in history:
        body = tickets.preview_of(m.text, m.content_type)[:300] or "-"
        if m.direction == "in":
            has_media = has_media or m.content_type != "text"
            lines.append(f"👤 {fmt_dt(m.created_at)}: {html.escape(body)}")
        else:
            if m.admin_id not in names:
                names[m.admin_id] = short_name(await session.get(User, m.admin_id)) if m.admin_id else "бот"
            lines.append(f"🛡 {names[m.admin_id]} · {fmt_dt(m.created_at)}: {html.escape(body)}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "…"
    rows = [[cb(texts.CHAT_BTN, T("reply", ticket.id)),
             cb("✅ Закрыть" if ticket.status != "closed" else "♻️ Открыть снова", T("close", ticket.id))]]
    second = [cb("👤 Клиент", AdminCb(s="u", a="card", id=ticket.user_id))]
    if ticket.order_id:
        second.append(cb(f"📦 Заказ #{ticket.order_id}", AdminCb(s="o", a="card", id=ticket.order_id)))
    rows.append(second)
    if has_media:
        rows.append([cb("📎 Показать вложения", T("media", ticket.id))])
    if ticket.kind == "appeal" and user is not None and user.is_banned and level >= 2:
        rows.append([cb("✅ Разбанить", T("unban", ticket.id))])
    rows.append([cb("⬅️ К обращениям", T("list", v="o")), menu_btn()])
    return text, ik(*rows)


async def show_ticket(call: CallbackQuery, session: AsyncSession, config: Config, ticket_id: int,
                      note: str = "") -> None:
    ticket = await tickets.get(session, ticket_id)
    if ticket is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    text, kb = await ticket_view(session, ticket, config.admin_level(call.from_user.id))
    await show(call, (f"{note}\n\n" if note else "") + text, kb)


async def send_ticket(message: Message, session: AsyncSession, config: Config, ticket_id: int, note: str = "") -> None:
    ticket = await tickets.get(session, ticket_id)
    if ticket is None:
        await message.answer(texts.NOT_FOUND)
        return
    text, kb = await ticket_view(session, ticket, config.admin_level(message.from_user.id))
    await message.answer((f"{note}\n\n" if note else "") + text, reply_markup=kb)


# ------------------------------------------------------------------ список и просмотр
@router.callback_query(AdminCb.filter((F.s == "t") & F.a.in_({"", "list"})))
async def t_list(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await call.answer()
    text, kb = await inbox_view(session, max(callback_data.p, 0), callback_data.v != "a")
    await show(call, text, kb)


@router.callback_query(_f("open"))
async def t_open(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                 config: Config) -> None:
    await state.clear()
    await call.answer()
    await show_ticket(call, session, config, callback_data.id)


@router.callback_query(_f("media"))
async def t_media(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    ticket = await tickets.get(session, callback_data.id)
    if ticket is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    sent = 0
    for m in await tickets.messages(session, ticket.id, 30):
        if m.direction == "in" and m.content_type != "text" and m.user_msg_id:
            try:
                await bot.copy_message(call.from_user.id, ticket.user_id, m.user_msg_id)
                sent += 1
            except Exception:  # noqa: BLE001 - клиент удалил сообщение
                pass
    if not sent:
        await call.message.answer("Вложения недоступны: возможно, клиент удалил сообщения.")


# ------------------------------------------------------------------ ответ
@router.callback_query(_f("reply"))
async def t_reply(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    """«✉️ Ответить» - режим чата с клиентом: по обращению, а если оно про заказ - по заказу."""
    target = await ticket_target(session, callback_data.id)
    if target is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await open_chat(call.message, session, call.from_user.id, target, state)


async def staff_replied(message: Message, session: AsyncSession) -> dict | bool:
    """Админ ответил свайпом на уведомление бота - найдём, к чему оно относится."""
    r = message.reply_to_message
    if r is None or message.from_user is None or r.from_user is None or not r.from_user.is_bot:
        return False
    row = await lookup(session, message.from_user.id, r.message_id)
    return {"amsg": row} if row is not None else False


@router.message(StateFilter(None), staff_replied)
async def t_swipe_reply(message: Message, session: AsyncSession, sessionmaker: async_sessionmaker, bot: Bot,
                        config: Config, amsg) -> None:  # noqa: ANN001
    """Свайп-ответ на уведомление: доставляем так же, как в режиме чата (альбом - целиком), с подтверждением."""
    now = now_utc()
    target = None
    if amsg.kind == "ticket":
        target = await ticket_target(session, amsg.ref_id)
    elif amsg.kind in ("order", "dispute"):
        order = await svc.get_order(session, amsg.ref_id)
        target = Target(order.user_id, order.id) if order is not None else None
    elif amsg.kind == "payment":
        pay = await session.get(Payment, amsg.ref_id)
        if pay is not None:
            ticket = (await tickets.open_or_append(session, pay.user_id, "pay", now))[0]
            target = Target(pay.user_id, None, ticket.id)
    else:
        uid = amsg.user_id or amsg.ref_id
        if uid:
            ticket = (await tickets.open_or_append(session, uid, "support", now))[0]
            target = Target(uid, None, ticket.id)
    if target is None:
        await message.answer(texts.NOT_FOUND)
        return
    await session.commit()
    await conversation.relay_or_buffer(bot, session, sessionmaker, config, message, target, chat_mode=False)


# ------------------------------------------------------------------ закрыть / разбанить
@router.callback_query(_f("close"))
async def t_close(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                  config: Config) -> None:
    ticket = await tickets.get(session, callback_data.id)
    if ticket is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    admin = await session.get(User, call.from_user.id)
    now = now_utc()
    if ticket.status == "closed":
        ticket.status, ticket.closed_at, ticket.closed_by = "answered", None, None
        await session.commit()
        await call.answer("Обращение снова открыто")
    else:
        await tickets.close(session, ticket, call.from_user.id, now)
        await call.answer("Закрыто ☑️")
        await mark_headers(bot, session, config, "ticket", ticket.id,
                           f"☑️ Закрыто: {short_name(admin)} · {to_local(now):%H:%M}",
                           markup_for=lambda lvl: closed_kb(ticket, lvl))
        for chat in await conversation.finish_ticket(session, ticket.id):  # чат по закрытому обращению - всё
            await safe_send(bot, chat.admin_id, texts.CHAT_FINISHED.format(mark=Target.of(chat).mark, n=chat.sent))
    if callback_data.v != "n":
        await show_ticket(call, session, config, ticket.id)


@router.callback_query(_f("unban"))
async def t_unban(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                  config: Config) -> None:
    if config.admin_level(call.from_user.id) < 2:
        await call.answer(texts.ADMIN_L2_ONLY, show_alert=True)
        return
    ticket = await tickets.get(session, callback_data.id)
    user = await session.get(User, ticket.user_id, populate_existing=True) if ticket else None
    if ticket is None or user is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    now = now_utc()
    if user.is_banned:
        user.is_banned, user.ban_reason = False, None
        log_admin(session, call.from_user.id, "unban", "user", user.id, via="appeal", ticket=ticket.id)
        await session.commit()
        await safe_send(bot, user.id, texts.USER_UNBANNED, reply_markup=main_menu(False))
    await tickets.close(session, ticket, call.from_user.id, now)
    admin = await session.get(User, call.from_user.id)
    await mark_headers(bot, session, config, "ticket", ticket.id,
                       f"✅ Разбанен(а): {short_name(admin)} · {to_local(now):%H:%M}",
                       markup_for=lambda lvl: closed_kb(ticket, lvl))
    await call.answer("Разбанен ✅")
    if callback_data.v != "n":
        await show_ticket(call, session, config, ticket.id)
