"""«Это задание?» — фото, файл или текст, присланные вне оформления заказа и вне поддержки.

В 2.2 такие сообщения получали «не понял» и терялись. Теперь каждое сразу сохраняется (stray_messages), а клиент
получает вопрос «📥 Оформить заказ / 💬 Отправить в поддержку» (альбом — одним вопросом):
• «📥 Оформить заказ» — фото и файлы ложатся в условие черновика (создаём, если его нет), текст — в комментарий,
  и оформление продолжается с нужного шага;
• «💬 Отправить в поддержку» — всё уходит команде обычным обращением (как /support).
Нет ответа за stray_timeout_minutes (по умолчанию 10 мин) — планировщик сам передаёт сообщения в поддержку
(flush_stray), а клиенту пишет, что ничего не потерялось."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.bot import drafts
from app.bot import files as upl
from app.bot.callbacks import StrayCb
from app.bot.handlers import order as wizard
from app.bot.keyboards import cb, ik, main_menu
from app.bot.notify import safe_send
from app.bot.support import Item, content_type, items_to_staff, message_text
from app.bot.ui import drop_kb
from app.config import Config
from app.db.models import StrayMessage, User
from app.services import orders as svc
from app.services import slots, tickets
from app.services.locks import app_lock
from app.services.settings import RuntimeSettings, load_settings
from app.utils.timeutil import now_utc

log = logging.getLogger(__name__)
router = Router(name="stray")


def attachable(it: StrayMessage) -> bool:
    """Можно положить в заказ: текст — в комментарий, фото и PDF/картинки — в условие."""
    return it.content_type == "text" or (it.file_type in ("photo", "document") and bool(it.file_id))


def _record(message: Message, user_id: int, now: datetime) -> StrayMessage:
    sm = StrayMessage(user_id=user_id, chat_id=message.chat.id, message_id=message.message_id,
                      media_group_id=message.media_group_id, content_type=content_type(message),
                      text=message_text(message), status="pending", created_at=now)
    f = upl.user_file(message)
    if f is not None:  # фото или документ, который годится в условие заказа
        sm.file_type, sm.file_id, sm.file_unique_id = f.file_type, f.file_id, f.unique_id
        sm.file_name, sm.mime_type, sm.size = f.file_name, f.mime_type, f.size
    elif message.document is not None:  # файл другого типа — только в поддержку
        sm.file_name, sm.mime_type = message.document.file_name, message.document.mime_type
    return sm


async def pending(session: AsyncSession, user_id: int, upto: int | None = None) -> list[StrayMessage]:
    q = select(StrayMessage).where(StrayMessage.user_id == user_id, StrayMessage.status == "pending")
    if upto is not None:
        q = q.where(StrayMessage.id <= upto)
    return list((await session.execute(q.order_by(StrayMessage.id)
                                       .execution_options(populate_existing=True))).scalars().all())


async def claim(session: AsyncSession, user_id: int, upto: int | None, status: str, now: datetime
                ) -> list[StrayMessage]:
    """Забрать ждущие сообщения (атомарно для бота: кнопка и таймер не обработают их дважды). Коммитит."""
    async with app_lock():
        rows = await pending(session, user_id, upto)
        for it in rows:
            it.status, it.resolved_at = status, now
        await session.commit()
    return rows


async def _drop_prompts(bot: Bot, rows: list[StrayMessage]) -> None:
    for chat_id, mid in {(it.chat_id, it.prompt_message_id) for it in rows if it.prompt_message_id}:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=None)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


# ------------------------------------------------------------------ сообщение вне сценариев
@router.message(StateFilter(None), F.photo | F.document | (F.text & ~F.text.startswith("/")))
async def stray_message(message: Message, session: AsyncSession, sessionmaker: async_sessionmaker, bot: Bot,
                        db_user: User, s: RuntimeSettings) -> None:
    now = now_utc()
    session.add(_record(message, db_user.id, now))
    await session.commit()
    user_id, chat_id, minutes = db_user.id, message.chat.id, int(s.stray_timeout_minutes)

    async def ask() -> None:  # один вопрос на пачку (альбом, несколько сообщений подряд)
        async with sessionmaker() as s2:
            rows = await pending(s2, user_id)
            if not rows:
                return
            await _drop_prompts(bot, rows)
            last = rows[-1].id
            kb = ik([cb(texts.STRAY_BTN_ORDER, StrayCb(action="order", id=last))]
                    if any(attachable(it) for it in rows) else None,
                    [cb(texts.STRAY_BTN_SUPPORT, StrayCb(action="support", id=last))])
            prompt = await bot.send_message(chat_id, texts.STRAY_ASK.format(minutes=minutes), reply_markup=kb)
            for it in rows:
                it.prompt_message_id = prompt.message_id
            await s2.commit()

    upl.schedule_ack(user_id, "stray", ask)


# ------------------------------------------------------------------ в поддержку
async def to_support(bot: Bot, session: AsyncSession, config: Config, user: User, rows: list[StrayMessage],
                     now: datetime, note: str) -> int:
    """Сообщения → обращение в поддержку (как /support) и уведомление команде. Коммитит. Возвращает id обращения."""
    ticket, created = await tickets.open_or_append(session, user.id, "support", now)
    for it in rows:
        tickets.add_in(session, ticket, it.content_type, it.text, it.message_id, now)
        it.ticket_id = ticket.id
    await items_to_staff(bot, session, config, ticket, user,
                         [Item(it.chat_id, it.message_id, it.content_type, it.text) for it in rows],
                         new=created, note=note)
    await session.commit()
    return ticket.id


@router.callback_query(StrayCb.filter(F.action == "support"))
async def stray_support(call: CallbackQuery, callback_data: StrayCb, session: AsyncSession, bot: Bot,
                        config: Config, db_user: User) -> None:
    rows = await claim(session, db_user.id, callback_data.id, "support", now_utc())
    await drop_kb(call)
    if not rows:
        await call.answer(texts.STRAY_DONE_ALREADY)
        return
    await call.answer()
    await to_support(bot, session, config, db_user, rows, now_utc(), texts.STRAY_ADMIN_NOTE)
    await call.message.answer(texts.STRAY_TO_SUPPORT, reply_markup=main_menu())


# ------------------------------------------------------------------ в заказ
@router.callback_query(StrayCb.filter(F.action == "order"))
async def stray_order(call: CallbackQuery, callback_data: StrayCb, state: FSMContext, session: AsyncSession,
                      bot: Bot, config: Config, db_user: User, s: RuntimeSettings) -> None:
    now = now_utc()
    rows = await claim(session, db_user.id, callback_data.id, "order", now)
    await drop_kb(call)
    if not rows:
        await call.answer(texts.STRAY_DONE_ALREADY)
        return
    await call.answer()
    await state.clear()
    drafts.mark_shown()
    draft = await svc.get_draft(session, db_user.id)
    if draft is None:
        draft = await svc.create_draft(session, db_user.id, None, now)
    photos, text_saved, rest = 0, False, []
    for it in rows:
        if it.content_type == "text" and (it.text or "").strip():
            comment = "\n".join(x for x in ((draft.comment or "").strip(), it.text.strip()) if x)
            draft.comment = comment[:wizard.MAX_COMMENT]
            text_saved = True
        elif it.file_id and it.file_type in ("photo", "document"):
            f = upl.IncomingFile(it.file_type, it.file_id, it.file_unique_id or it.file_id, it.file_name,
                                 it.mime_type, it.size)
            if await wizard.save_file(bot, config, session, draft.id, "condition", f, db_user.id):
                photos += 1
        else:
            rest.append(it)
        it.order_id = draft.id
    await session.commit()
    if rest:  # не подходит в условие (например, .docx) — в поддержку, чтобы не потерялось
        for it in rest:
            it.status = "support"
        await to_support(bot, session, config, db_user, rest, now, texts.STRAY_ADMIN_NOTE)
        await call.message.answer(texts.STRAY_NOT_ATTACHED)
    await drafts.refresh_menu(session, db_user.id)
    note = texts.STRAY_TO_ORDER.format(n=photos) if photos else texts.STRAY_TO_ORDER_TEXT if text_saved else None
    intake = await slots.intake_state(session, s, now, unlimited=db_user.unlimited_active(now))
    if not intake.open:  # приём закрыт — черновик сохранён, вернуться можно позже
        await call.message.answer("\n\n".join(x for x in (note, wizard.closed_text(intake)) if x),
                                  reply_markup=wizard.remind_kb())
        return
    step = draft.wizard_step if draft.wizard_step in wizard.STATES else "photos"
    if step == "terms":
        step = "summary"
    step = await wizard.sane_step(session, draft, step, s)
    await wizard.goto(step, call.message, state, session, draft, s, db_user, note=note)


# ------------------------------------------------------------------ планировщик: без ответа — в поддержку
async def flush_stray(bot: Bot, config: Config, sessionmaker: async_sessionmaker[AsyncSession],
                      now: datetime | None = None) -> int:
    """Раз в минуту: «Это задание?» без ответа дольше stray_timeout_minutes (по последнему сообщению пачки) —
    передаём в поддержку и сообщаем клиенту. Возвращает, скольким клиентам."""
    now = now or now_utc()
    done = 0
    async with sessionmaker() as session:
        s = await load_settings(session)
        minutes = int(s.stray_timeout_minutes)
        cutoff = now - timedelta(minutes=minutes)
        users = list((await session.execute(select(StrayMessage.user_id).where(StrayMessage.status == "pending")
                                            .distinct())).scalars().all())
        for user_id in users:
            rows = await pending(session, user_id)
            if not rows or max(it.created_at for it in rows) > cutoff:
                continue
            rows = await claim(session, user_id, rows[-1].id, "auto", now)
            user = await session.get(User, user_id)
            if not rows or user is None:
                continue
            await _drop_prompts(bot, rows)
            await to_support(bot, session, config, user, rows, now, texts.STRAY_ADMIN_AUTO.format(minutes=minutes))
            await safe_send(bot, rows[0].chat_id, texts.STRAY_AUTO)
            done += 1
    return done
