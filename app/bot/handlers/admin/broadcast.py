"""Админка → рассылка: сегмент → сообщение → предпросмотр → подтверждение → отправка ≤25 сообщений/с → отчёт."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.base import admin_router, cancel_btn, menu_btn
from app.bot.keyboards import cb, ik
from app.bot.notify import safe_send
from app.bot.states import AdminSG
from app.bot.ui import drop_kb, show
from app.db.models import Broadcast
from app.services.admin_log import log_admin
from app.services.broadcast import SEGMENTS, run_broadcast, segment_user_ids
from app.utils.timeutil import now_utc

log = logging.getLogger(__name__)
router = admin_router("admin_broadcast", level=2)
_tasks: set[asyncio.Task] = set()  # держим ссылки, чтобы задачи не собрал GC


def _f(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "b") & (F.a == action))


@router.callback_query(_f(""))
async def b_menu(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await call.answer()
    now = now_utc()
    rows = []
    for key, title in SEGMENTS.items():
        n = len(await segment_user_ids(session, key, now))
        rows.append([cb(f"{title}: {n}", AdminCb(s="b", a="seg", v=key))])
    await show(call, "📣 <b>Рассылка</b>\nКому отправляем? (без забаненных и заблокировавших бота)",
               ik(*rows, [menu_btn()]))


@router.callback_query(_f("seg"))
async def b_segment(call: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    if callback_data.v not in SEGMENTS:
        await call.answer("Нет такого сегмента")
        return
    await state.set_state(AdminSG.broadcast_message)
    await state.update_data(segment=callback_data.v)
    await call.answer()
    await call.message.answer(f"Сегмент: <b>{SEGMENTS[callback_data.v]}</b>.\nПришли сообщение для рассылки: текст, "
                              "фото, видео, файл. Сначала покажу предпросмотр.", reply_markup=ik([cancel_btn()]))


@router.message(AdminSG.broadcast_message)
async def b_message(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    segment = (await state.get_data()).get("segment", "all")
    await state.clear()
    b = Broadcast(admin_id=message.from_user.id, segment=segment, from_chat_id=message.chat.id,
                  message_id=message.message_id, status="draft", created_at=now_utc())
    session.add(b)
    await session.commit()
    n = len(await segment_user_ids(session, segment, now_utc()))
    await message.answer("👀 <b>Предпросмотр</b>: так увидят получатели:")
    await bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    await message.answer(f"Отправить {n} получателям («{SEGMENTS.get(segment, segment)}»)?",
                         reply_markup=ik([cb("🚀 Отправить", AdminCb(s="b", a="send", id=b.id)),
                                          cb("✖️ Отмена", AdminCb(s="b", a="cancel", id=b.id))]))


async def _run_and_report(bot: Bot, sessionmaker: async_sessionmaker, broadcast_id: int, chat_id: int) -> None:
    try:
        b = await run_broadcast(bot, sessionmaker, broadcast_id)
        await safe_send(bot, chat_id, f"📣 Рассылка #{b.id} завершена.\nДоставлено: <b>{b.delivered}</b> из {b.total} · "
                                      f"заблокировали бота: {b.blocked} · ошибок: {b.failed}")
    except Exception:  # noqa: BLE001
        log.exception("рассылка %s упала", broadcast_id)
        await safe_send(bot, chat_id, f"⚠️ Рассылка #{broadcast_id} прервалась с ошибкой: смотри логи.")


@router.callback_query(_f("send"))
async def b_send(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                 sessionmaker: async_sessionmaker) -> None:
    res = await session.execute(update(Broadcast).where(Broadcast.id == callback_data.id, Broadcast.status == "draft")
                                .values(status="queued"))
    if res.rowcount != 1:
        await session.rollback()
        await call.answer("Эта рассылка уже отправлена или отменена.", show_alert=True)
        return
    log_admin(session, call.from_user.id, "broadcast", "broadcast", callback_data.id)
    await session.commit()
    await call.answer("Поехали 🚀")
    await drop_kb(call)
    await call.message.answer("🚀 Рассылка запущена: отчёт пришлём сюда, когда закончим.")
    task = asyncio.create_task(_run_and_report(bot, sessionmaker, callback_data.id, call.from_user.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.callback_query(_f("cancel"))
async def b_cancel(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    await session.execute(update(Broadcast).where(Broadcast.id == callback_data.id, Broadcast.status == "draft")
                          .values(status="cancelled"))
    await session.commit()
    await call.answer("Отменено")
    await drop_kb(call)
