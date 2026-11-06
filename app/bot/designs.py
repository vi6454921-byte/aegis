"""v2.4: «🎨 Файлы и дизайн»: общие куски бота без хендлеров (их зовут оплата, переписка и карточки).

- notify_paid: заказ оплачен (или принят по безлимиту): клиенту «передали исполнителю», админам уведомление
  с типом, оформлением, количеством и заданием, кнопка «✉️ Ответить»; заказ появляется во «📥 Входящих»;
- on_result: админ прислал клиенту файл по заказу: заказ готов, клиенту «✅ Принять» / «🔁 Нужны правки»."""
from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app import actionlog, texts
from app.bot import files as upl
from app.bot.callbacks import AdminCb, DesignCb
from app.bot.keyboards import cb, ik
from app.bot.notify import mark_headers, notify_admins, remember_all, safe_send, send_files, staff_targets
from app.config import Config
from app.db.models import Order, User
from app.services import design as dsvc
from app.services import orders as svc
from app.services.users import display_name, short_name
from app.utils.money import fmt_rub
from app.utils.timeutil import now_utc, to_local

log = logging.getLogger(__name__)


def admin_kb(order_id: int) -> InlineKeyboardMarkup:
    """Под уведомлением о заказе: сначала «✉️ Ответить» (так админ и присылает готовый файл)."""
    return ik([cb(texts.CHAT_BTN, AdminCb(s="c", a="o", id=order_id))],
              [cb("📦 Открыть заказ", AdminCb(s="o", a="open", id=order_id)),
               cb("🖼 Файлы", AdminCb(s="o", a="files", id=order_id))])


def result_kb(order_id: int) -> InlineKeyboardMarkup:
    return ik([cb(texts.DESIGN_ACCEPT_BTN, DesignCb(a="accept", id=order_id)),
               cb(texts.DESIGN_REDO_BTN, DesignCb(a="redo", id=order_id))])


def admin_text(order: Order, user: User | None, n_files: int, plan: str = "") -> str:
    lines = [texts.ADMIN_NEW_DESIGN.format(id=order.id, user=display_name(user) if user else order.user_id,
                                           what=dsvc.what(order), test=" 🧪 тест" if order.is_test else "")]
    style = dsvc.order_style(order)
    if style:
        lines.append(texts.ADMIN_DESIGN_STYLE.format(style=style))
        if order.design_style == "ssau":
            lines.append(texts.ADMIN_DESIGN_SSAU.format(url=dsvc.SSAU_URL))
    if order.unlimited:
        lines.append(texts.ADMIN_DESIGN_UNLIMITED)
    else:
        lines.append(texts.ADMIN_DESIGN_PAID.format(price=fmt_rub(order.price_final or 0),
                                                    plan=f" ({plan})" if plan else ""))
    lines.append(texts.ADMIN_DESIGN_FILES.format(n=n_files))
    if order.comment:
        lines.append(f"💬 <i>{html.escape(order.comment[:1500])}</i>")
    if user is not None and user.is_priority:
        lines.append("⭐ приоритет")
    lines += ["", texts.ADMIN_DESIGN_HINT, texts.ADMIN_REPLY_HINT.strip()]
    return "\n".join(lines)


async def notify_paid(bot: Bot, session: AsyncSession, config: Config, order: Order, user: User | None,
                      plan: str = "", autosend_files: bool = True, tell_user: bool = True) -> None:
    """Клиенту: оплачено и передано исполнителю. Админам: файлы задания (если включено) и уведомление.
    Заказ (статус «оплачен») появляется во «📥 Входящих». Коммитит (запоминает уведомления)."""
    if tell_user:
        tmpl = texts.DESIGN_UNLIMITED if order.unlimited else texts.DESIGN_PAID
        await safe_send(bot, order.user_id, tmpl.format(id=order.id, what=dsvc.describe(order)))
    files = await svc.files_of(session, order.id, "condition")
    text = admin_text(order, user, len(files), plan)
    if autosend_files and files:
        for admin_id, _lvl in staff_targets(config, 1):
            try:
                await send_files(bot, admin_id, files, caption=f"{texts.DESIGN_FILES_TITLE} · #{order.id}")
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                log.warning("файлы заказа #%s админу %s не ушли: %s", order.id, admin_id, e)
    sent = await notify_admins(bot, config, text[:4096], markup_for=lambda _lvl: admin_kb(order.id))
    remember_all(session, sent, "order", order.id, order.user_id, text[:4096])
    await session.commit()


def is_result(content_types: list[str]) -> bool:
    return any(ct in dsvc.RESULT_TYPES for ct in content_types)


async def on_result(bot: Bot, session: AsyncSession, config: Config, admin_id: int, order: Order) -> Order:
    """Админ прислал клиенту файл (pptx, pdf, png, jpg…) по заказу раздела: заказ готов, клиенту после файлов
    (один раз на пачку) - «✅ Принять» / «🔁 Нужны правки». Коммитит."""
    now = now_utc()
    order, became = await dsvc.mark_delivered(session, order.id, now, admin_id)
    if order.status != "done":
        return order
    if became:
        admin = await session.get(User, admin_id)
        actionlog.action(f"отправил результат по заказу #{order.id} ({dsvc.describe_plain(order)})", admin)
        await mark_headers(bot, session, config, "order", order.id, texts.DESIGN_SENT_MARK.format(
            who=f"{short_name(admin) if admin else admin_id} · {to_local(now):%H:%M}"),
            markup_for=lambda _lvl, oid=order.id: admin_kb(oid))
    user_id, order_id, what = order.user_id, order.id, dsvc.describe(order)

    async def follow_up() -> None:
        await safe_send(bot, user_id, texts.DESIGN_RESULT.format(id=order_id, what=what),
                        reply_markup=result_kb(order_id))

    upl.schedule_ack(admin_id, f"design:{order_id}", follow_up)
    return order
