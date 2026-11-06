"""Поддержка для клиента: /support, /paysupport, вопрос по заказу, ответы на сообщения команды, возврат рублей."""
from __future__ import annotations

import html
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot import files as upl
from app.bot.callbacks import MenuCb, SupCb
from app.bot.handlers.common import menu_router
from app.bot.keyboards import cb, ik, main_menu
from app.bot.states import SupportSG
from app.bot.support import MAX_TEXT, content_type, copyable, message_text, to_staff
from app.config import Config
from app.db.models import Order, User
from app.services import tickets
from app.utils.timeutil import now_utc

router = Router(name="support")
# v2.3: ответ свайпом на сообщение команды - из любого шага (подключается сразу после админки, до сценариев)
reply_router = Router(name="support_reply")
TOPIC_KIND = {"support": "support", "general": "support", "pay": "pay", "order": "order", "appeal": "appeal"}
_albums: dict[str, tuple[int, float]] = {}  # media_group_id → (обращение, время): у альбома один заголовок


async def start_support(message: Message, state: FSMContext, config: Config, topic: str, order_id: int = 0,
                        ticket_id: int = 0) -> None:
    kind = TOPIC_KIND.get(topic, "support")
    await state.set_state(SupportSG.message)
    await state.update_data(topic=kind, order_id=order_id, ticket_id=ticket_id)
    if ticket_id:
        text = "💬 Напиши ответ одним сообщением (можно с фото или файлом): передадим команде."
    elif kind == "pay":
        text = texts.PAYSUPPORT_PROMPT
    elif kind == "order":
        text = texts.ORDER_QUESTION_PROMPT.format(id=order_id)
    else:
        text = texts.SUPPORT_PROMPT
    if config.support_contact and not ticket_id:
        text += "\n" + texts.SUPPORT_CONTACT_LINE.format(contact=html.escape(config.support_contact))
    await message.answer(text, reply_markup=ik([cb(texts.BTN_CANCEL, MenuCb(action="support_cancel"))]))


@menu_router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext, config: Config) -> None:
    await start_support(message, state, config, "support")


@menu_router.message(Command("paysupport"))
async def cmd_paysupport(message: Message, state: FSMContext, config: Config) -> None:
    await start_support(message, state, config, "pay")


@router.callback_query(MenuCb.filter(F.action.in_({"support", "paysupport"})))
async def cb_support(call: CallbackQuery, callback_data: MenuCb, state: FSMContext, config: Config) -> None:
    await call.answer()
    await start_support(call.message, state, config, "pay" if callback_data.action == "paysupport" else "support")


@router.callback_query(SupCb.filter(F.action == "reply"))
async def cb_reply(call: CallbackQuery, callback_data: SupCb, state: FSMContext, session: AsyncSession,
                   db_user: User, config: Config) -> None:
    ticket = await tickets.get(session, callback_data.id)
    await call.answer()
    if ticket is None or ticket.user_id != db_user.id:
        await start_support(call.message, state, config, "support")
        return
    await start_support(call.message, state, config, ticket.kind, ticket.order_id or 0, ticket.id)


@router.callback_query(SupCb.filter(F.action == "order"))
async def cb_order_question(call: CallbackQuery, callback_data: SupCb, state: FSMContext, session: AsyncSession,
                            db_user: User, config: Config) -> None:
    order = await session.get(Order, callback_data.id)
    await call.answer()
    if order is None or order.user_id != db_user.id:
        await start_support(call.message, state, config, "support")
        return
    await start_support(call.message, state, config, "order", order.id)


@router.callback_query(MenuCb.filter(F.action == "support_cancel"))
async def cb_support_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(texts.CANCELLED, reply_markup=main_menu())


async def deliver(message: Message, session: AsyncSession, db_user: User, bot: Bot, config: Config, kind: str,
                  order_id: int = 0, ticket_id: int = 0, *, note: str | None = None,
                  only: set[int] | None = None) -> bool:
    """Записывает сообщение в обращение и шлёт админам (only - только этим). False - сообщение не подходит
    (уже ответили клиенту)."""
    body = message_text(message) or ""
    if not copyable(message) or (content_type(message) == "text" and not body.strip()):
        await message.answer("Пришли текст, фото или файл 🙂")
        return False
    if len(body) > MAX_TEXT:
        await message.answer(f"Слишком длинно: уложись в {MAX_TEXT} символов или разбей на части.")
        return False
    now = now_utc()
    album = message.media_group_id
    ticket, created, header = None, False, True
    if album and album in _albums:
        ticket, header = await tickets.get(session, _albums[album][0]), False
    if ticket is None and ticket_id:
        ticket = await tickets.get(session, ticket_id)
        if ticket is not None and ticket.user_id != db_user.id:
            ticket = None
    if ticket is None:
        ticket, created = await tickets.open_or_append(session, db_user.id, kind, now, order_id or None)
    if album:
        _albums[album] = (ticket.id, time.monotonic())
        if len(_albums) > 2000:
            cutoff = time.monotonic() - 600
            for key in [k for k, (_, t) in _albums.items() if t < cutoff]:
                _albums.pop(key, None)
    tickets.add_in(session, ticket, content_type(message), body, message.message_id, now)
    await to_staff(bot, session, config, ticket, db_user, message, new=created, header=header, note=note, only=only)
    await session.commit()
    return True


@router.message(SupportSG.message)
async def support_message(message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot,
                          config: Config) -> None:
    data = await state.get_data()
    ok = await deliver(message, session, db_user, bot, config, data.get("topic") or "support",
                       int(data.get("order_id") or 0), int(data.get("ticket_id") or 0))
    if not ok:
        return

    async def ack() -> None:
        await state.clear()
        await bot.send_message(message.chat.id, texts.SUPPORT_SENT, reply_markup=main_menu())

    if message.media_group_id:
        upl.schedule_ack(db_user.id, "support", ack)
    else:
        await ack()


async def replied_ticket(message: Message, session: AsyncSession) -> dict | bool:
    """Клиент ответил свайпом на сообщение команды - найдём обращение."""
    r = message.reply_to_message
    if r is None or message.from_user is None:
        return False
    ticket = await tickets.by_user_reply(session, message.from_user.id, r.message_id)
    return {"reply_ticket": ticket} if ticket is not None else False


@reply_router.message(replied_ticket)
async def support_by_reply(message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot,
                           config: Config, reply_ticket) -> None:  # noqa: ANN001
    """Свайп-ответ на сообщение команды - в ту же переписку (по заказу - в переписку заказа), из любого шага:
    черновик заказа и другие вводы не сбиваются, а ответ «💬 Ответить» (SupportSG) считается отправленным."""
    if await state.get_state() == SupportSG.message.state:
        await state.clear()
    if await deliver(message, session, db_user, bot, config, reply_ticket.kind, reply_ticket.order_id or 0,
                     reply_ticket.id) and not message.media_group_id:
        await message.answer(texts.SUPPORT_SENT)


async def album_continues(message: Message, state: FSMContext) -> dict | bool:
    """Остальные фото альбома, начатого свайп-ответом (Telegram помечает ответом не каждое фото альбома)."""
    album = message.media_group_id
    if not album or album not in _albums or await state.get_state() == SupportSG.message.state:
        return False
    return {"album_ticket_id": _albums[album][0]}


@reply_router.message(album_continues)
async def support_album_rest(message: Message, session: AsyncSession, db_user: User, bot: Bot, config: Config,
                             album_ticket_id: int) -> None:
    await deliver(message, session, db_user, bot, config, "support", 0, album_ticket_id)
