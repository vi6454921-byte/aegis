"""Админка → «✖️ Отмена» в любом вводе: сбросить ввод и вернуть экран, с которого начали (на том же месте)."""
from __future__ import annotations

import logging

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import files as upl
from app.bot.callbacks import AdminCb
from app.bot.handlers.admin import (dbq, inbox, manage, menu, orders, promocodes, receipts, settings, staff, tasks,
                                    tickets, users)
from app.bot.handlers.admin.base import admin_router, back_of
from app.bot.ui import drop_kb, show
from app.config import Config
from app.db.models import Payment, ProfileClaim, PromoPostReport
from app.services import tickets as tsvc
from app.services.settings import GROUPS, RuntimeSettings

log = logging.getLogger(__name__)
router = admin_router("admin_cancel")
View = tuple[str, InlineKeyboardMarkup] | None


async def back_view(session: AsyncSession, s: RuntimeSettings, config: Config, actor_id: int,
                    b: AdminCb) -> View:
    level = config.admin_level(actor_id)
    if b.s == "o":
        if b.id:
            return await orders.card_view(session, b.id, config, level)
        return await orders.list_view(session, b.v or "q", b.p)
    if b.s == "u":
        if b.id:
            return await users.card_view(session, b.id, config, level)
        return await users.list_view(session, b.p, config, level)
    if b.s == "t":
        if b.id:
            ticket = await tsvc.get(session, b.id)
            return await tickets.ticket_view(session, ticket, level) if ticket else None
        return await tickets.inbox_view(session, b.p, b.v != "a")
    if b.s == "pay":
        payment = await session.get(Payment, b.id) if b.id else None
        return await receipts.view(session, payment) if payment else await receipts.list_view(session)
    if b.s == "m":
        return await menu.menu_view(session, s, level, actor_id)
    if b.s == "in":
        return await inbox.inbox_view(session, b.v == "today", b.p)
    if b.s == "pt":
        if b.a == "post" and b.id:
            return await tasks.post_view(session, b.id, level)
        if b.a == "pc" and b.id:
            claim = await session.get(ProfileClaim, b.id, populate_existing=True)
            return await tasks.claim_view(session, claim, s) if claim else None
        if b.a == "rp" and b.id:
            report = await session.get(PromoPostReport, b.id, populate_existing=True)
            return await tasks.report_view(session, report, s) if report else None
        return await tasks.queue_view(session)
    if level < 2:
        return None
    if b.s == "st":
        if b.id:
            return await staff.card_view(session, config, actor_id, b.id)
        return await staff.list_view(session, config, actor_id)
    if b.s == "s":
        return settings.group_view(b.v, s) if b.v in GROUPS else settings.groups_view()
    if b.s == "g":
        return manage.hub_view()
    if b.s == "vip":
        return await manage.vip_user_view(session, b.id) if b.id else manage.vip_hub_view()
    if b.s == "bn":
        if b.id:
            return await manage.ban_user_view(session, config, actor_id, b.id)
        return await manage.ban_hub_view(session)
    if b.s == "gv":
        return manage.give_hub_view()
    if b.s == "db":
        return dbq.hub_view()
    if b.s == "pc":
        return await promocodes.hub_view(session, b.v or "all", b.p)
    return None


@router.callback_query(AdminCb.filter(F.s == "x"))
async def x_cancel(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                   s: RuntimeSettings, config: Config) -> None:
    await state.clear()
    upl.cancel_ack(call.from_user.id, "solution")
    await call.answer("Отменено")
    back = back_of(callback_data)
    view = await back_view(session, s, config, call.from_user.id, back) if back else None
    if view is not None:
        await show(call, *view)
        return
    msg = call.message
    if isinstance(msg, Message) and msg.text is not None:  # вопрос новым сообщением — оставляем пометку
        try:
            await msg.edit_text("✖️ Отменено.", reply_markup=None)
            return
        except TelegramBadRequest as e:
            log.debug("x_cancel: %s", e)
    await drop_kb(call)
