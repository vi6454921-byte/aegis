"""Админка → «📥 Входящие»: всё, что ждёт нас, одним списком — заказы на проверке, споры и обращения без ответа.

Сначала новые. «📅 За сегодня» — только то, что пришло сегодня (по местному времени). Заказ открывается
карточкой вместе с фото условия (у спора — со скринами замечания), обращение — своей карточкой с перепиской.
Счётчик — в «Требует внимания» на главном экране /admin."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb
from app.bot.handlers.admin import orders as admin_orders
from app.bot.handlers.admin.base import admin_router, menu_btn
from app.bot.keyboards import cb, ik, pager
from app.bot.notify import send_files
from app.bot.ui import show
from app.config import Config
from app.db.models import Order, Ticket, User
from app.services import orders as svc
from app.services import tickets
from app.services.users import short_name
from app.utils.timeutil import fmt_dt, local_day_bounds, local_today, now_utc

router = admin_router("admin_inbox")
PER_PAGE = 10
ORDER_STATUSES = ("review", "dispute")
ICONS = {"review": "🔎", "dispute": "⚠️"}
WHAT = texts.INBOX_WHAT


@dataclass
class Entry:
    kind: str  # order | ticket
    id: int
    at: datetime
    line: str
    button: str


def _order_time(o: Order) -> datetime:
    return (o.updated_at if o.status == "dispute" else o.submitted_at) or o.updated_at or o.created_at


async def count(session: AsyncSession) -> int:
    """Сколько всего во «Входящих»: заказы на проверке и споры + обращения, ждущие ответа."""
    n_orders = int(await session.scalar(select(func.count(Order.id)).where(Order.status.in_(ORDER_STATUSES))) or 0)
    return n_orders + await tickets.open_count(session)


async def entries(session: AsyncSession, since: datetime | None = None) -> list[Entry]:
    out: list[Entry] = []
    rows = (await session.execute(select(Order, User).join(User, User.id == Order.user_id)
                                  .where(Order.status.in_(ORDER_STATUSES)))).all()
    for o, u in rows:
        at = _order_time(o)
        if since is not None and at < since:
            continue
        what = WHAT[o.status] + (" · ♾" if o.unlimited else "")
        subject = texts.SUBJECTS.get(o.subject or "", "—")
        out.append(Entry("order", o.id, at,
                         texts.INBOX_ORDER_LINE.format(icon=ICONS[o.status], id=o.id, what=f"{subject} · {what}",
                                                       user=short_name(u), when=fmt_dt(at)),
                         f"{ICONS[o.status]} #{o.id} · {short_name(u)} · {WHAT[o.status]}"))
    trows = (await session.execute(select(Ticket, User).join(User, User.id == Ticket.user_id)
                                   .where(Ticket.status == "open"))).all()
    for t, u in trows:
        at = t.last_in_at or t.updated_at or t.created_at
        if since is not None and at < since:
            continue
        preview = (t.preview or "").replace("\n", " ")
        out.append(Entry("ticket", t.id, at,
                         texts.INBOX_TICKET_LINE.format(title=tickets.title(t), user=short_name(u), when=fmt_dt(at))
                         + (f" — «{preview[:60]}»" if preview else ""),
                         f"💬 №{t.id} · {short_name(u)} · {preview[:24]}"))
    out.sort(key=lambda e: e.at, reverse=True)
    return out


async def inbox_view(session: AsyncSession, today: bool, page: int) -> tuple[str, InlineKeyboardMarkup]:
    since = local_day_bounds(local_today(now_utc()))[0] if today else None
    items = await entries(session, since)
    v = "today" if today else ""
    switch = cb(texts.INBOX_ALL_BTN if today else texts.INBOX_TODAY_BTN,
                AdminCb(s="in", v="" if today else "today"))
    refresh = cb(texts.INBOX_REFRESH_BTN, AdminCb(s="in", v=v, p=page))
    if not items:
        return (texts.INBOX_EMPTY_TODAY if today else texts.INBOX_EMPTY), ik([switch, refresh], [menu_btn()])
    pages = max((len(items) + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(max(page, 0), pages - 1)
    chunk = items[page * PER_PAGE:(page + 1) * PER_PAGE]
    lines = [texts.INBOX_TITLE.format(n=len(items), period=texts.INBOX_PERIOD_TODAY if today else ""), ""]
    lines += [e.line for e in chunk]
    buttons = [[cb(e.button[:60], AdminCb(s="in", a="o", id=e.id) if e.kind == "order"
                   else AdminCb(s="t", a="open", id=e.id))] for e in chunk]
    text = "\n".join(lines)
    return (text if len(text) <= 4000 else text[:3990] + "…"), ik(
        *buttons, pager(page, len(items), PER_PAGE, lambda p: AdminCb(s="in", v=v, p=p)),
        [switch, refresh], [menu_btn()])


@router.callback_query(AdminCb.filter((F.s == "in") & (F.a == "")))
async def in_list(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await call.answer()
    text, kb = await inbox_view(session, callback_data.v == "today", callback_data.p)
    await show(call, text, kb)


@router.callback_query(AdminCb.filter((F.s == "in") & (F.a == "o")))
async def in_order(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                   bot: Bot, config: Config) -> None:
    """Заказ из «Входящих»: сначала фото (условие, у спора — скрины замечания), под ними — карточка с кнопками."""
    order = await svc.get_order(session, callback_data.id)
    if order is None or order.status == "draft":
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await state.clear()
    await call.answer()
    for kind, title in admin_orders.FILE_TITLES:
        if kind == "solution" or (kind == "dispute" and order.status != "dispute"):
            continue
        files = await svc.files_of(session, order.id, kind)
        if files:
            await send_files(bot, call.from_user.id, files, caption=texts.INBOX_FILES_CAPTION.format(title=title, id=order.id))
    await admin_orders.show_card(call, session, order.id, config, new=True)
