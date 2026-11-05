"""Забаненные пользователи: бот закрыт, но можно обжаловать бан — сообщение уходит админам как обращение."""
from __future__ import annotations

import time
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import MenuCb, SupCb
from app.bot.filters import IsBanned
from app.bot.keyboards import cb, ik
from app.bot.states import AppealSG
from app.bot.support import content_type, copyable, message_text, to_staff
from app.config import Config
from app.db.models import User
from app.services import tickets
from app.utils.timeutil import fmt_dt, now_utc

router = Router(name="banned")
router.message.filter(IsBanned(), ~F.successful_payment)
router.callback_query.filter(IsBanned())

APPEAL_COOLDOWN = timedelta(hours=12)
APPEAL_MAX = 2000
NAG_EVERY = 30.0  # сек: на прочие сообщения отвечаем не чаще
_nagged: dict[int, float] = {}


def appeal_kb():  # noqa: ANN201
    return ik([cb(texts.APPEAL_BTN, MenuCb(action="appeal"))])


def banned_text(user: User) -> str:
    reason = texts.BANNED_REASON.format(reason=user.ban_reason) if user.ban_reason else ""
    return texts.BANNED.format(reason=reason)


async def show_banned(message: Message, user: User) -> None:
    if message.text in texts.MAIN_MENU.values() or message.text == texts.ADMIN_MENU_BTN:
        await message.answer(texts.BANNED_KB_REMOVED, reply_markup=ReplyKeyboardRemove())
    await message.answer(banned_text(user), reply_markup=appeal_kb())


async def can_appeal(session: AsyncSession, user: User) -> bool:
    """Новое обращение — раз в 12 часов, но сразу, если команда уже ответила на предыдущее."""
    if user.appeal_at is None or now_utc() - user.appeal_at >= APPEAL_COOLDOWN:
        return True
    last = await tickets.last_appeal(session, user.id)
    return last is not None and last.answered_at is not None and last.answered_at >= user.appeal_at


async def start_appeal(message: Message, state: FSMContext, session: AsyncSession, user: User,
                       ticket_id: int = 0) -> None:
    if not ticket_id and not await can_appeal(session, user):
        await message.answer(texts.APPEAL_COOLDOWN.format(sent=fmt_dt(user.appeal_at),
                                                          next=fmt_dt(user.appeal_at + APPEAL_COOLDOWN)))
        return
    await state.set_state(AppealSG.message)
    await state.update_data(ticket_id=ticket_id)
    await message.answer(texts.APPEAL_PROMPT, reply_markup=ik([cb(texts.BTN_CANCEL, MenuCb(action="appeal_cancel"))]))


@router.message(CommandStart())
async def banned_start(message: Message, state: FSMContext, db_user: User) -> None:
    await state.clear()
    await show_banned(message, db_user)


@router.message(Command("support", "paysupport", "help"))
async def banned_support(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await start_appeal(message, state, session, db_user)


@router.callback_query(MenuCb.filter(F.action == "appeal"))
async def cb_appeal(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await call.answer()
    await start_appeal(call.message, state, session, db_user)


@router.callback_query(SupCb.filter(F.action == "reply"))
async def cb_appeal_reply(call: CallbackQuery, callback_data: SupCb, state: FSMContext, session: AsyncSession,
                          db_user: User) -> None:
    ticket = await tickets.get(session, callback_data.id)
    await call.answer()
    await start_appeal(call.message, state, session, db_user,
                       ticket.id if ticket is not None and ticket.user_id == db_user.id else 0)


@router.callback_query(MenuCb.filter(F.action == "appeal_cancel"))
async def cb_appeal_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer(texts.CANCELLED)
    await call.message.edit_reply_markup(reply_markup=None)


async def submit_appeal(message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot,
                        config: Config, ticket_id: int = 0) -> None:
    body = message_text(message) or ""
    if not copyable(message) or (content_type(message) == "text" and not body.strip()):
        await message.answer(texts.APPEAL_NEED_CONTENT)
        return
    if len(body) > APPEAL_MAX:
        await message.answer(texts.APPEAL_TOO_LONG.format(n=APPEAL_MAX))
        return
    now = now_utc()
    ticket = await tickets.get(session, ticket_id) if ticket_id else None
    created = False
    if ticket is None or ticket.user_id != db_user.id:
        ticket, created = await tickets.open_or_append(session, db_user.id, "appeal", now)
    tickets.add_in(session, ticket, content_type(message), body, message.message_id, now)
    db_user.appeal_at = now
    db_user.appeals_count = (db_user.appeals_count or 0) + 1
    await to_staff(bot, session, config, ticket, db_user, message, new=created)
    await session.commit()
    await state.clear()
    await message.answer(texts.APPEAL_SENT)


@router.message(AppealSG.message)
async def appeal_message(message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot,
                         config: Config) -> None:
    data = await state.get_data()
    await submit_appeal(message, state, session, db_user, bot, config, int(data.get("ticket_id") or 0))


async def replied_ticket(message: Message, session: AsyncSession) -> dict | bool:
    r = message.reply_to_message
    if r is None or message.from_user is None:
        return False
    ticket = await tickets.by_user_reply(session, message.from_user.id, r.message_id)
    return {"reply_ticket": ticket} if ticket is not None else False


@router.message(StateFilter(None), replied_ticket)
async def appeal_by_reply(message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot,
                          config: Config, reply_ticket) -> None:  # noqa: ANN001
    await submit_appeal(message, state, session, db_user, bot, config, reply_ticket.id)


@router.message()
async def banned_any(message: Message, db_user: User) -> None:
    last = _nagged.get(db_user.id, 0.0)
    if time.monotonic() - last < NAG_EVERY:
        return
    _nagged[db_user.id] = time.monotonic()
    if len(_nagged) > 10000:
        _nagged.clear()
    await show_banned(message, db_user)


@router.callback_query()
async def banned_any_callback(call: CallbackQuery) -> None:
    await call.answer(texts.BANNED_SHORT, show_alert=True)
