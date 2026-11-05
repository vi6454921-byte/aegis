"""Админка → «✉️ Ответить»: режим чата с клиентом по заказу (или по обращению без заказа).

Кнопка «✉️ Ответить» в карточке заказа, в карточке обращения и под уведомлением о новом заказе включает режим
(строка в admin_chats — переживает перезапуск бота). Дальше всё, что админ присылает боту вне других вводов
админки, уходит клиенту с пометкой «По заказу №…», а под сообщением админа — «✅ Доставлено» или понятная ошибка.
Режим действует до «✅ Завершить» (или «📦 Готово — отправить клиенту»). Другие кнопки его не ломают: пока идёт
ввод цены или поиск пользователя, сообщение уходит туда, следующее — снова клиенту. В /admin — «↩️ Вернуться
в чат по заказу №…». Свайп-ответы на уведомления разбирает admin/tickets.py (он подключён раньше)."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.bot import conversation
from app.bot.callbacks import AdminCb
from app.bot.conversation import Target
from app.bot.handlers.admin import orders
from app.bot.handlers.admin.base import admin_router, close_prompt, menu_btn
from app.bot.keyboards import cb, ik
from app.bot.states import AdminSG
from app.bot.ui import drop_kb
from app.config import Config
from app.db.models import AdminChat
from app.services import orders as svc
from app.services import tickets
from app.services.settings import RuntimeSettings

router = admin_router("admin_chat")


def _c(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "c") & (F.a == action))


async def order_target(session: AsyncSession, order_id: int) -> Target | None:
    order = await svc.get_order(session, order_id) if order_id else None
    if order is None or order.status == "draft":
        return None
    return Target(order.user_id, order.id)


async def ticket_target(session: AsyncSession, ticket_id: int) -> Target | None:
    """Обращение по заказу — это переписка по заказу (одна ветка); остальные — само обращение."""
    ticket = await tickets.get(session, ticket_id) if ticket_id else None
    if ticket is None:
        return None
    if ticket.order_id:
        target = await order_target(session, ticket.order_id)
        if target is not None:
            return target
    return Target(ticket.user_id, None, ticket.id)


async def open_chat(message: Message, session: AsyncSession, admin_id: int, target: Target,
                    state: FSMContext) -> AdminChat | None:
    """Включить режим и показать заголовок новым сообщением (уведомления и карточки остаются как были)."""
    await state.clear()
    previous = await conversation.start(session, admin_id, target)
    chat = await conversation.get_chat(session, admin_id)
    if chat is None:
        return None
    text, kb = await conversation.header_view(session, chat, texts.CHAT_SWITCHED.format(old=previous)
                                              if previous else "")
    await message.answer(text, reply_markup=kb)
    return chat


@router.callback_query(_c("o"))
@router.callback_query(AdminCb.filter((F.s == "o") & (F.a == "msg")))  # «✉️ Написать клиенту» на карточках из 2.2
async def c_order(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    target = await order_target(session, callback_data.id)
    if target is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await open_chat(call.message, session, call.from_user.id, target, state)


@router.callback_query(_c("t"))
async def c_ticket(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    target = await ticket_target(session, callback_data.id)
    if target is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await open_chat(call.message, session, call.from_user.id, target, state)


@router.callback_query(_c("end"))
async def c_end(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    target = Target(0, callback_data.id, None) if callback_data.v == "o" else Target(0, None, callback_data.id)
    finished = await conversation.finish(session, call.from_user.id, target)
    await drop_kb(call)
    if finished is None:
        await call.answer(texts.CHAT_NOT_ACTIVE)
        return
    await call.answer(texts.CHAT_FINISHED_TOAST)
    done = Target.of(finished)
    back = (cb(texts.CHAT_ORDER_BTN.format(id=done.order_id), AdminCb(s="o", a="open", id=done.order_id))
            if done.order_id else
            cb(texts.CHAT_TICKET_BTN.format(id=done.ticket_id), AdminCb(s="t", a="open", id=done.ticket_id or 0)))
    await call.message.answer(texts.CHAT_FINISHED.format(mark=done.mark, n=finished.sent),
                              reply_markup=ik([back, menu_btn()]))


@router.callback_query(_c("done"))
async def c_done(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                 s: RuntimeSettings, bot: Bot, config: Config) -> None:
    """«📦 Готово — отправить клиенту» из чата: выдать заказ и завершить чат (карточка — новым сообщением)."""
    await orders.finish_order(call, callback_data.id, state, session, s, bot, config, new=True)


@router.callback_query(_c("back"))
async def c_back(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    chat = await conversation.get_chat(session, call.from_user.id)
    if chat is None:
        await call.answer(texts.CHAT_NOT_ACTIVE, show_alert=True)
        return
    await state.clear()
    await call.answer()
    text, kb = await conversation.header_view(session, chat)
    await call.message.answer(text, reply_markup=kb)


# ------------------------------------------------------------------ сообщения админа → клиенту
async def in_chat(message: Message, session: AsyncSession) -> dict | bool:
    """Админ в режиме чата (команды «/…» сюда не относятся — их разбирают свои хендлеры или «не понял»)."""
    if message.from_user is None or (message.text or "").startswith("/"):
        return False
    chat = await conversation.get_chat(session, message.from_user.id)
    return {"admin_chat": chat} if chat is not None else False


@router.message(StateFilter(None), in_chat)
async def c_relay(message: Message, session: AsyncSession, sessionmaker: async_sessionmaker, bot: Bot,
                  config: Config, admin_chat: AdminChat) -> None:
    await conversation.relay_or_buffer(bot, session, sessionmaker, config, message, Target.of(admin_chat),
                                       chat_mode=True)


@router.message(StateFilter(AdminSG.ticket_reply, AdminSG.order_message))
async def c_legacy(message: Message, state: FSMContext, session: AsyncSession, sessionmaker: async_sessionmaker,
                   bot: Bot, config: Config) -> None:
    """Ввод из 2.2 («Ответ клиенту» / «Сообщение клиенту» ждали сообщение) — включаем режим чата и доставляем."""
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    order_id, ticket_id = int(data.get("order_id") or 0), int(data.get("ticket_id") or 0)
    target = await order_target(session, order_id) if order_id else await ticket_target(session, ticket_id)
    if target is None:
        await message.answer(texts.NOT_FOUND)
        return
    if await open_chat(message, session, message.from_user.id, target, state) is None:
        return
    await conversation.relay_or_buffer(bot, session, sessionmaker, config, message, target, chat_mode=True)
