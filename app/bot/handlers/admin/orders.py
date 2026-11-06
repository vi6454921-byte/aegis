"""Админка → заказы: очередь, карточка, конспекты, цена, счёт, работа, решение, объяснение, переписка, спор, возврат.

1 уровень: проверка, счёт, напоминание, отклонение, работа, решение, «✉️ Ответить» (режим чата - admin/chat.py).
2 уровень дополнительно: ручная цена и возврат денег.
v2.3: заказ по безлимиту (промокод) - вместо счёта «✅ Принять в очередь (0 ₽)»; в карточке - последние сообщения
переписки по заказу и «💬 Переписка» целиком (кто, что и когда).
"""
from __future__ import annotations

import html
import json
import logging
import shutil
from pathlib import Path

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot import conversation, designs
from app.bot import files as upl
from app.bot.callbacks import AdminCb, MyOrderCb, PayCb, SupCb
from app.bot.handlers.admin.base import L2, admin_router, ask, cancel_btn, close_prompt, menu_btn
from app.bot.handlers.my_orders import done_kb, send_solution_files
from app.bot.handlers.payments import plan_text
from app.bot.keyboards import Btn, any_alert_kb, cb, ik, order_alert_kb, pager, solution_button
from app.bot.notify import mark_headers, safe_send, send_files
from app.bot.render import notes_line, order_card, order_line, price_formula, status_label
from app.bot.states import AdminSG
from app.bot.ui import drop_kb, show
from app.config import Config
from app.db.models import AdminLog, Order, Payment, Referral, Ticket, User
from app.payments.stars_provider import StarsPaymentProvider
from app.services import design as dsvc
from app.services import orders as svc
from app.services import payments as paysvc
from app.services import solutions, tickets
from app.services.admin_log import log_admin
from app.services.ledger import available_bonus
from app.services.pricing import split_payment
from app.services.referrals import RewardResult
from app.services.settings import RuntimeSettings
from app.services.users import display_name, short_name
from app.utils.money import fmt_rub, parse_rub, stars_for
from app.utils.timeutil import fmt_date, fmt_dt, now_utc, to_local

log = logging.getLogger(__name__)
router = admin_router("admin_orders")

FILTERS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "q": ("Очередь", svc.ACTIVE),
    "ev": ("На оценке", (svc.ESTIMATE, svc.QUOTED)),  # v2.4: «📎 Любая задача»
    "rv": ("На проверке", ("review",)),
    "ap": ("Ждут оплаты", ("awaiting_payment",)),
    "pd": ("Оплачены", ("paid",)),
    "iw": ("В работе", ("in_work",)),
    "dn": ("Выданы", ("done",)),
    "ds": ("Споры", ("dispute",)),
    "cl": ("Закрытые", ("accepted", "refunded", "rejected", "cancelled")),
    "all": ("Все", None),
}
PER_PAGE = 8
SOLUTION_MAX = 20 * 1024 * 1024
FILE_TITLES = (("condition", "📄 Условие"), ("lecture", "📗 Лекция"), ("practice", "📘 Практика"),
               ("dispute", "⚠️ Скрин замечания"), ("solution", "🧩 Решение"))
UPLOAD_PROMPT = (
    "📤 <b>Решение для заказа #{id}</b>\n"
    "Пришли HTML-файл(ы) или ZIP документом (до 20 МБ каждый). В ZIP можно положить index.html "
    "вместе с картинками и скриптами: откроется как сайт.\nКогда всё отправишь, нажми «💾 Сохранить»."
)
HISTORY = {
    "issue_invoice": "💳 выставил(а) счёт", "set_price": "✏️ поменял(а) цену", "set_task_count": "🔢 поменял(а) число задач",
    "note_approve": "✅ одобрил(а) конспект", "note_reject": "❌ не принял(а) конспект",
    "back_to_review": "↩️ вернул(а) на проверку", "reject_order": "🚫 отклонил(а) заказ",
    "take_in_work": "🛠 взял(а) в работу", "upload_solution": "📤 загрузил(а) решение",
    "mark_done": "📦 выдал(а) клиенту", "redo": "🔁 отправил(а) на переделку", "refund": "↩️ сделал(а) возврат",
    "pay_reminder": "⏰ напомнил(а) об оплате", "message_client": "✉️ написал(а) клиенту",
    "promo_unlimited": "♾ принял(а) по безлимиту",
    "manual_confirm": "🧾 подтвердил(а) оплату переводом", "manual_reject": "🧾 не подтвердил(а) оплату",
    "quote_price": "💰 назначил(а) цену",
    "design_delivered": "📦 отправил(а) результат клиенту",
}


def A(a: str, order_id: int, v: str = "") -> AdminCb:  # noqa: N802
    return AdminCb(s="o", a=a, id=order_id, v=v)


def _cb(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "o") & (F.a == action))


# ------------------------------------------------------------------ экраны
async def list_view(session: AsyncSession, f: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    if f not in FILTERS:
        f = "q"
    title, statuses = FILTERS[f]
    rows, total = await svc.admin_list(session, statuses, page, PER_PAGE)
    counts = await svc.status_counts(session)
    lines = [f"📥 <b>Заказы: {title}</b> ({total})"]
    if statuses and f != "cl":
        lines.append("<i>По дедлайну; при равном дне ⭐ приоритетные выше</i>")
    buttons = []
    for o, u in rows:
        lines.append(f"{order_line(o, u.is_priority)} · {short_name(u)}")
        buttons.append([cb(f"#{o.id} · {status_label(o)} · {fmt_rub(o.price or 0)}",
                           A("card", o.id))])
    if not rows:
        lines.append("Пусто 👌")

    def label(key: str, name: str, sts: tuple[str, ...] | None) -> str:
        n = sum(counts.values()) if sts is None else sum(counts.get(x, 0) for x in sts)
        return ("• " if key == f else "") + name + (f" · {n}" if n else "")

    flt = [cb(label(k, name, sts), AdminCb(s="o", a="list", v=k)) for k, (name, sts) in FILTERS.items()]
    return "\n".join(lines), ik(*buttons, pager(page, total, PER_PAGE, lambda p: AdminCb(s="o", a="list", v=f, p=p)),
                                flt[:3], flt[3:6], flt[6:9], flt[9:], [menu_btn()])


def card_kb(order: Order, config: Config, level: int, ticket_id: int | None = None) -> InlineKeyboardMarkup:
    rows: list[list] = []
    st, oid = order.status, order.id
    explain = svc.is_explain(order)
    if dsvc.is_design(order):  # v2.4: «🎨 Файлы и дизайн»: проверки нет, результат - через «✉️ Ответить»
        if st == "awaiting_payment":
            rows.append([cb("⏰ Напомнить об оплате", A("remind", oid)), cb("🚫 Отклонить", A("reject", oid))])
        elif st == "paid":
            rows.append([cb("🛠 Взять в работу", A("take", oid))])
    elif st == svc.ESTIMATE:  # v2.4: «📎 Любая задача» ждёт оценки
        rows.append([cb(texts.QUOTE_BTN, A("quote", oid)), cb(texts.ANY_REJECT_BTN, A("reject", oid))])
    elif st == svc.QUOTED:
        rows.append([cb(texts.QUOTE_CHANGE_BTN, A("quote", oid)), cb("⏰ Напомнить об оплате", A("remind", oid))])
        rows.append([cb(texts.ANY_REJECT_BTN, A("reject", oid))])
    elif st == "review":
        if not explain and not order.unlimited:  # по безлимиту 0 ₽ - скидка за конспекты не нужна
            if order.lecture_status != "none":
                rows.append([cb("✅ Лекция", A("note", oid, "l1")), cb("❌ Лекция", A("note", oid, "l0"))])
            if order.practice_status != "none":
                rows.append([cb("✅ Практика", A("note", oid, "p1")), cb("❌ Практика", A("note", oid, "p0"))])
        tools = [cb("✏️ Цена", A("price", oid))] if level >= 2 and not order.unlimited else []
        if not explain:
            tools.append(cb("🔢 Кол-во задач", A("count", oid)))
        rows.append(tools)
        rows.append([cb(texts.ADMIN_ACCEPT_UNLIMITED_BTN, A("invoice", oid)) if order.unlimited
                     else cb(f"💳 Выставить счёт · {fmt_rub(order.price or 0)}", A("invoice", oid))])
        rows.append([cb("🚫 Отклонить", A("reject", oid))])
    elif st == "awaiting_payment":
        rows.append([cb("⏰ Напомнить об оплате", A("remind", oid))])
        rows.append([cb("↩️ Вернуть на проверку", A("review", oid)), cb("🚫 Отклонить", A("reject", oid))])
    elif st == "paid":
        rows.append([cb("🛠 Взять в работу", A("take", oid))])
        if not explain:  # объяснение пишут в чате - «✉️ Ответить» ниже
            rows.append([cb("📤 Загрузить решение", A("upload", oid))])
    elif st == "in_work":
        if explain:
            rows.append([cb("📎 Заменить файл" if order.solution_entry else "📎 Приложить файл (HTML/ZIP)",
                            A("upload", oid))])
        else:
            rows.append([cb("📤 Заменить решение" if order.solution_entry else "📤 Загрузить решение",
                            A("upload", oid))])
        if svc.can_finish(order):
            rows.append([cb(texts.CHAT_READY_BTN, A("done", oid))])
    elif st == "done":
        if not explain or order.solution_entry:
            rows.append([cb("📤 Заменить решение", A("upload", oid))])
    elif st == "dispute":
        rows.append([cb("🔁 Переделать", A("redo", oid))])
    if st in svc.REFUNDABLE and level >= 2:
        rows.append([cb("↩️ Вернуть деньги", A("refund", oid))])
    if order.solution_entry and order.solution_token and config.webapp_url:
        rows.append([Btn(text="👁 Посмотреть решение",
                         web_app=WebAppInfo(url=f"{config.webapp_url}/s/{order.solution_token}"))])
    if ticket_id:
        rows.append([cb("📨 Вопрос клиента: ответить", AdminCb(s="t", a="open", id=ticket_id))])
    rows.append([cb(texts.CHAT_BTN, AdminCb(s="c", a="o", id=oid)), cb(texts.CHAT_HISTORY_BTN, A("chat", oid))])
    rows.append([cb("🖼 Файлы", A("files", oid)), cb("👤 Клиент", AdminCb(s="u", a="card", id=order.user_id)),
                 cb("📜 История", A("log", oid))])
    rows.append([cb("⬅️ К очереди", AdminCb(s="o", a="list", v="q")), menu_btn()])
    return ik(*rows)


async def card_view(session: AsyncSession, order_id: int, config: Config, level: int = 2
                    ) -> tuple[str, InlineKeyboardMarkup] | None:
    order = await svc.get_order(session, order_id)
    if order is None or order.status == "draft":
        return None
    user = await session.get(User, order.user_id)
    assignee = await session.get(User, order.assignee_id) if order.assignee_id else None
    counts = {k: await svc.count_files(session, order.id, k) for k, _ in FILE_TITLES}
    text = order_card(order, counts, user, admin=True, assignee=assignee)
    extra = []
    if counts["dispute"]:
        extra.append(f"⚠️ Скринов замечания: {counts['dispute']}")
    pay = await paysvc.paid_payment_for(session, order.id)
    if pay is None:
        pay = await session.scalar(select(Payment).where(Payment.order_id == order.id, Payment.status == "refunded")
                                   .order_by(Payment.id.desc()).limit(1))
    if pay is not None:
        extra.append(f"Платёж #{pay.id}: {pay.provider}, {fmt_rub(pay.amount_rub)}"
                     + (f" = {pay.amount_stars} ⭐" if pay.amount_stars else "")
                     + (f" · {pay.status}" if pay.status != "paid" else "") + (" 🧪" if pay.is_test else ""))
    question = await session.scalar(select(Ticket).where(Ticket.order_id == order.id, Ticket.status == "open")
                                    .order_by(Ticket.id.desc()).limit(1))
    if question is not None:
        extra.append(f"💬 Клиент ждёт ответа: «{html.escape((question.preview or '')[:120])}»")
    if dsvc.is_design(order) and order.status in ("paid", "in_work"):
        if order.design_style == "ssau":
            extra.append(texts.ADMIN_DESIGN_SSAU.format(url=dsvc.SSAU_URL))
        extra.append(texts.ADMIN_DESIGN_HINT)
    if extra:
        text += "\n" + "\n".join(extra)
    talk, total = await tickets.order_thread(session, order.id, 5)
    if talk:  # кто, что и когда - последние сообщения переписки по заказу
        excerpt = "\n".join([texts.CHAT_CARD_TITLE.format(n=len(talk), total=total)]
                            + await conversation.history_lines(session, talk, 120))
        if len(text) + len(excerpt) < 4000:
            text += "\n" + excerpt
    return text, card_kb(order, config, level, question.id if question else None)


async def show_card(call: CallbackQuery, session: AsyncSession, order_id: int, config: Config,
                    note: str = "", new: bool = False) -> None:
    view = await card_view(session, order_id, config, config.admin_level(call.from_user.id))
    if view is None:
        await call.message.answer(texts.NOT_FOUND)
        return
    text, kb = view
    if note:
        text = f"{note}\n\n{text}"
    if new:
        await call.message.answer(text, reply_markup=kb)
    else:
        await show(call, text, kb)


async def answer_card(message: Message, session: AsyncSession, order_id: int, config: Config, note: str = "") -> None:
    view = await card_view(session, order_id, config, config.admin_level(message.from_user.id))
    if view is None:
        await message.answer(texts.NOT_FOUND)
        return
    await message.answer((f"{note}\n\n" if note else "") + view[0], reply_markup=view[1])


async def mark_order(bot: Bot, session: AsyncSession, config: Config, order_id: int, marker: str) -> None:
    """Пометка на уведомлениях о заказе у всех админов: «💳 Счёт выставлен: Аня · 14:05»."""
    order = await svc.get_order(session, order_id)
    quote = svc.is_any(order) and order.status in (svc.ESTIMATE, svc.QUOTED)
    design = dsvc.is_design(order)
    await mark_headers(bot, session, config, "order", order_id, marker,
                       markup_for=lambda _lvl: any_alert_kb(order_id) if quote
                       else designs.admin_kb(order_id) if design else order_alert_kb(order_id))


def who(admin: User | None, admin_id: int) -> str:
    return f"{short_name(admin) if admin else admin_id} · {to_local(now_utc()):%H:%M}"


async def pay_details(session: AsyncSession, order: Order, s: RuntimeSettings) -> list[str]:
    """Как спишется оплата: бонусы, баланс, остаток счётом (звёзды или перевод)."""
    user = await session.get(User, order.user_id)
    details = []
    split = split_payment(order.price_final or 0, await available_bonus(session, user.id, now_utc()),
                          user.balance_rub, int(s.bonus_max_share_pct))
    if split.bonus or split.balance:
        details.append(plan_text(split.bonus, split.balance, split.invoice))
    if s.payments_mode == "stars" and split.invoice:
        details.append(f"Счётом: <b>{stars_for(split.invoice, s.rate)} ⭐</b> (Telegram Stars)")
    elif s.payments_mode == "manual" and split.invoice:
        details.append("Оплата переводом по реквизитам: покажем после нажатия кнопки.")
    return details


async def notify_quote(bot: Bot, session: AsyncSession, order: Order, s: RuntimeSettings) -> bool:
    """v2.4: клиенту «Мы оценили задачу: 350 ₽» и кнопки «Оплатить» / «Отказаться»."""
    details = await pay_details(session, order, s)
    price = fmt_rub(order.price_final or 0)
    sent = await safe_send(bot, order.user_id, texts.QUOTE_READY.format(
        price=price, id=order.id, details=("\n\n" + "\n".join(details)) if details else ""),
        reply_markup=ik([cb(f"{texts.PAY_BTN} · {price}", PayCb(action="order", id=order.id))],
                        [cb(texts.QUOTE_DECLINE_BTN, MyOrderCb(action="cancel", id=order.id))]))
    return sent is not None


async def notify_invoice(bot: Bot, session: AsyncSession, order: Order, s: RuntimeSettings) -> None:
    if order.status == "paid":
        tmpl = (texts.INVOICE_UNLIMITED if order.unlimited
                else texts.INVOICE_FREE if order.free_tasks else texts.INVOICE_ZERO)
        await safe_send(bot, order.user_id, tmpl.format(id=order.id))
        return
    details = [price_formula(order)]
    nl = None if svc.is_explain(order) else notes_line(
        order, await svc.count_files(session, order.id, "lecture"), await svc.count_files(session, order.id, "practice"))
    if nl:
        details.append(nl)
    details += await pay_details(session, order, s)
    await safe_send(bot, order.user_id, texts.INVOICE_READY.format(
        id=order.id, price=fmt_rub(order.price_final or 0), details="\n".join(details)),
        reply_markup=ik([cb(f"{texts.PAY_BTN} · {fmt_rub(order.price_final or 0)}", PayCb(action="order", id=order.id))]))


async def notify_reward(bot: Bot, reward: RewardResult, s: RuntimeSettings) -> None:
    if reward.amount:
        await safe_send(bot, reward.referrer_id, texts.REF_REWARD.format(amount=fmt_rub(reward.amount),
                                                                         date=fmt_date(reward.expires_at)))
    if reward.new_priority:
        await safe_send(bot, reward.referrer_id, texts.REF_PRIORITY.format(n=reward.paid_friends))
    if reward.new_free_month:
        await safe_send(bot, reward.referrer_id, texts.REF_FREE_MONTH.format(
            n=reward.paid_friends, date=fmt_date(reward.free_until), tasks=s.free_month_tasks))


def explain_done_kb(order: Order, config: Config) -> InlineKeyboardMarkup:
    """Кнопки под «объяснение готово»: понятно / задать вопрос (+ файл, если приложен)."""
    return ik([solution_button(config, order.solution_token, MyOrderCb(action="solution", id=order.id))]
              if order.solution_entry else None,
              [cb(texts.EXPLAIN_ACCEPT_BTN, MyOrderCb(action="accept", id=order.id)),
               cb(texts.EXPLAIN_ASK_BTN, SupCb(action="order", id=order.id))])


# ------------------------------------------------------------------ список и карточка
@router.callback_query(_cb("list"))
async def o_list(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await call.answer()
    text, kb = await list_view(session, callback_data.v or "q", max(callback_data.p, 0))
    await show(call, text, kb)


@router.callback_query(_cb("card"))
async def o_card(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                 config: Config) -> None:
    await state.clear()
    await call.answer()
    await show_card(call, session, callback_data.id, config)


@router.callback_query(_cb("open"))
async def o_open(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                 config: Config) -> None:
    """Карточка новым сообщением - из уведомлений (их не трогаем)."""
    await state.clear()
    await call.answer()
    await show_card(call, session, callback_data.id, config, new=True)


@router.callback_query(_cb("files"))
async def o_files(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    sent = False
    await call.answer()
    design = dsvc.is_design(await svc.get_order(session, callback_data.id))
    for kind, title in FILE_TITLES:
        files = await svc.files_of(session, callback_data.id, kind)
        if kind == "condition" and design:
            title = texts.DESIGN_FILES_TITLE
        if files:
            sent = True
            await send_files(bot, call.from_user.id, files, caption=f"{title} · заказ #{callback_data.id}")
    if not sent:
        await call.message.answer("Файлов нет.")


@router.callback_query(_cb("log"))
async def o_log(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    oid = callback_data.id
    order = await svc.get_order(session, oid)
    if order is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    pay_ids = [str(i) for i in (await session.execute(select(Payment.id).where(Payment.order_id == oid))).scalars()]
    cond = and_(AdminLog.target_type == "order", AdminLog.target_id == str(oid))
    if pay_ids:
        cond = or_(cond, and_(AdminLog.target_type == "payment", AdminLog.target_id.in_(pay_ids)))
    rows = (await session.execute(select(AdminLog).where(cond).order_by(AdminLog.id.desc()).limit(25))).scalars().all()
    lines = [f"📜 <b>История заказа #{oid}</b>",
             f"Создан {fmt_dt(order.created_at)} · отправлен {fmt_dt(order.submitted_at)}"
             + (f" · оплачен {fmt_dt(order.paid_at)}" if order.paid_at else "")]
    names: dict[int, str] = {}
    for r in reversed(rows):
        if r.admin_id not in names:
            names[r.admin_id] = short_name(await session.get(User, r.admin_id))
        what = HISTORY.get(r.action, r.action)
        detail = ""
        try:
            d = json.loads(r.details) if r.details else {}
        except ValueError:
            d = {}
        if r.action == "issue_invoice" and "price" in d:
            detail = f": {fmt_rub(int(d['price']))}"
        elif r.action == "set_price":
            detail = f": {fmt_rub(int(d['new']))}" if d.get("new") is not None else ": авто"
        elif r.action == "set_task_count" and "new" in d:
            detail = f": {d['new']}"
        elif r.action == "reject_order" and d.get("reason"):
            detail = f": {html.escape(str(d['reason']))[:80]}"
        elif r.action == "message_client" and d.get("delivered") is False:
            detail = " (не доставлено)"
        lines.append(f"• {fmt_dt(r.created_at)}, {names[r.admin_id]} {what}{detail}")
    if not rows:
        lines.append("\nДействий админов пока не было.")
    await call.answer()
    await show(call, "\n".join(lines), ik([cb("📦 К заказу", A("card", oid)), menu_btn()]))


# ------------------------------------------------------------------ проверка: конспекты, цена, количество
@router.callback_query(_cb("note"))
async def o_note(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, config: Config) -> None:
    which = {"l": "lecture", "p": "practice"}.get(callback_data.v[:1], "")
    approved = callback_data.v[1:] == "1"
    try:
        await svc.set_note_status(session, callback_data.id, which, approved, call.from_user.id)
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer(("Лекция" if which == "lecture" else "Практика") + (" одобрена ✅" if approved else " не принята ❌"))
    await show_card(call, session, callback_data.id, config)


@router.callback_query(_cb("price"), L2)
async def o_price(call: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    await call.answer()
    await ask(call, state, AdminSG.order_price,
              f"✏️ <b>Цена заказа #{callback_data.id}</b>\nПришли сумму в рублях, например <code>150</code>.\n"
              "<code>0</code>: бесплатно, <code>авто</code>, вернуть автоматический расчёт.",
              ik([cb("🔄 Авто", A("price_auto", callback_data.id)), cb("🎁 Бесплатно", A("price_zero", callback_data.id))],
                 [cancel_btn(A("card", callback_data.id))]),
              order_id=callback_data.id)


async def _set_price(session: AsyncSession, order_id: int, price: int | None, admin_id: int) -> str:
    try:
        await svc.set_price_override(session, order_id, price, admin_id)
    except svc.OrderError as e:
        return f"⚠️ {html.escape(str(e))}"
    return "✏️ Цена: " + ("автоматический расчёт" if price is None else fmt_rub(price))


@router.callback_query(AdminCb.filter((F.s == "o") & F.a.in_({"price_auto", "price_zero"})), L2)
async def o_price_quick(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                        config: Config) -> None:
    await state.clear()
    note = await _set_price(session, callback_data.id, None if callback_data.a == "price_auto" else 0,
                            call.from_user.id)
    await call.answer()
    await show_card(call, session, callback_data.id, config, note)


@router.message(AdminSG.order_price, L2)
async def o_price_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot,
                        config: Config) -> None:
    t = (message.text or "").strip().lower()
    data = await state.get_data()
    order_id = int(data.get("order_id") or 0)
    if t in ("авто", "auto"):
        price = None
    elif t.replace(",", ".") in ("0", "0.0", "0.00"):
        price = 0
    else:
        price = parse_rub(t)
        if price is None:
            await message.answer("Нужна сумма в рублях, например 150, или «авто».")
            return
    await state.clear()
    note = await _set_price(session, order_id, price, message.from_user.id)
    await close_prompt(bot, data)
    await answer_card(message, session, order_id, config, note)


@router.callback_query(_cb("count"))
async def o_count(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    order = await svc.get_order(session, callback_data.id)
    if order is None or order.status != "review" or svc.is_explain(order):
        await call.answer("Количество задач сейчас менять нельзя.", show_alert=True)
        return
    await call.answer()
    quick = [cb(str(n), A("count_go", order.id, str(n))) for n in range(1, 6) if n != order.task_count]
    await ask(call, state, AdminSG.order_count,
              f"🔢 <b>Сколько задач в заказе #{order.id}?</b> Сейчас: {order.task_count}.\n"
              f"Нажми число или пришли своё (от 1 до {svc.MAX_TASKS}). Цена пересчитается сама.",
              ik(quick, [cancel_btn(A("card", order.id))]), order_id=order.id)


async def _set_count(session: AsyncSession, order_id: int, n: int, admin_id: int) -> str:
    try:
        order = await svc.set_task_count(session, order_id, n, admin_id)
    except svc.OrderError as e:
        return f"⚠️ {html.escape(str(e))}"
    return f"🔢 Задач: {order.task_count}, цена пересчитана: {fmt_rub(order.price or 0)}"


@router.callback_query(_cb("count_go"))
async def o_count_go(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                     config: Config) -> None:
    await state.clear()
    note = await _set_count(session, callback_data.id, int(callback_data.v or 0), call.from_user.id)
    await call.answer()
    await show_card(call, session, callback_data.id, config, note)


@router.message(AdminSG.order_count)
async def o_count_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot,
                        config: Config) -> None:
    t = (message.text or "").strip()
    data = await state.get_data()
    order_id = int(data.get("order_id") or 0)
    if not t.isdigit() or not 1 <= int(t) <= svc.MAX_TASKS:
        await message.answer(f"Нужно число от 1 до {svc.MAX_TASKS}.")
        return
    await state.clear()
    note = await _set_count(session, order_id, int(t), message.from_user.id)
    await close_prompt(bot, data)
    await answer_card(message, session, order_id, config, note)


# ------------------------------------------------------------------ v2.4: оценка «📎 Любой задачи»
@router.callback_query(_cb("quote"))
async def o_quote(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    order = await svc.get_order(session, callback_data.id)
    if order is None or not svc.is_any(order) or order.status not in (svc.ESTIMATE, svc.QUOTED):
        await call.answer("Цену уже не назначить: статус заказа изменился.", show_alert=True)
        return
    await call.answer()
    await ask(call, state, AdminSG.order_quote, texts.QUOTE_PROMPT.format(id=order.id),
              ik([cancel_btn(A("card", order.id))]), order_id=order.id)


@router.message(AdminSG.order_quote)
async def o_quote_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Config,
                        s: RuntimeSettings) -> None:
    price = parse_rub((message.text or "").strip())
    if price is None:
        await message.answer(texts.QUOTE_BAD)
        return
    data = await state.get_data()
    order_id = int(data.get("order_id") or 0)
    try:
        order = await svc.quote(session, order_id, price, now_utc(), message.from_user.id)
    except svc.OrderError as e:
        await message.answer(html.escape(str(e)))
        if str(e) != texts.QUOTE_RANGE:
            await state.clear()
        return
    await state.clear()
    await close_prompt(bot, data)
    delivered = await notify_quote(bot, session, order, s)
    admin = await session.get(User, message.from_user.id)
    await mark_order(bot, session, config, order.id,
                     texts.QUOTE_MARK.format(price=fmt_rub(price), who=who(admin, message.from_user.id)))
    note = texts.QUOTE_DONE_NOTE.format(price=fmt_rub(price))
    if not delivered:
        note += "\n⚠️ Не доставлено: возможно, клиент заблокировал бота"
    await answer_card(message, session, order.id, config, note)


# ------------------------------------------------------------------ счёт и оплата
@router.callback_query(_cb("invoice"))
async def o_invoice(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings,
                    bot: Bot, config: Config) -> None:
    try:
        order = await svc.issue_invoice(session, callback_data.id, s, now_utc(), call.from_user.id)
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    free = order.status == "paid"
    await call.answer(texts.ADMIN_UNLIMITED_ACCEPTED_TOAST if order.unlimited
                      else "Бесплатно: заказ сразу в очереди ✅" if free else "Счёт отправлен клиенту ✅")
    await notify_invoice(bot, session, order, s)
    admin = await session.get(User, call.from_user.id)
    await mark_order(bot, session, config, order.id,
                     (texts.ADMIN_UNLIMITED_ACCEPTED_MARK.format(who=who(admin, call.from_user.id)) if order.unlimited
                      else f"🎁 Бесплатно, в очереди: {who(admin, call.from_user.id)}" if free
                      else f"💳 Счёт {fmt_rub(order.price_final or 0)} выставил(а): {who(admin, call.from_user.id)}"))
    await show_card(call, session, order.id, config)


@router.callback_query(_cb("remind"))
async def o_remind(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    order = await svc.get_order(session, callback_data.id)
    if order is None or order.status not in svc.PAYABLE:
        await call.answer("Заказ уже не ждёт оплаты.", show_alert=True)
        return
    ok = await safe_send(bot, order.user_id, texts.PAY_REMINDER.format(id=order.id, price=fmt_rub(order.price_final or 0)),
                         reply_markup=ik([cb(f"{texts.PAY_BTN} · {fmt_rub(order.price_final or 0)}",
                                             PayCb(action="order", id=order.id))]))
    order.pay_reminded_at = now_utc()
    log_admin(session, call.from_user.id, "pay_reminder", "order", order.id)
    await session.commit()
    await call.answer("Напомнили ✅" if ok else "Не доставлено: возможно, клиент заблокировал бота", show_alert=not ok)


@router.callback_query(_cb("review"))
async def o_back_to_review(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                           config: Config) -> None:
    try:
        order = await svc.back_to_review(session, callback_data.id, now_utc(), call.from_user.id)
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("Снова на проверке")
    await safe_send(bot, order.user_id, texts.BACK_TO_REVIEW.format(id=order.id))
    await show_card(call, session, order.id, config)


REJECT_REASONS = {
    "unclear": "Не видно или не хватает условия: пришли фото чётче и оформи заказ заново.",
    "subject": "Такие задачи мы пока не берём.",
    "late": "Не успеем к этому сроку: попробуй выбрать срок позже.",
}


@router.callback_query(_cb("reject"))
async def o_reject(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    oid = callback_data.id
    order = await svc.get_order(session, oid)
    if order is None or order.status not in svc.CANCELLABLE:
        await call.answer("Статус заказа уже изменился, открой его заново.", show_alert=True)
        return
    await call.answer()
    await ask(call, state, AdminSG.order_reject,
              texts.ANY_REJECT_PROMPT.format(id=oid) if svc.is_any(order)
              else f"🚫 <b>Отклонить заказ #{oid}</b>\n\nВыбери причину или напиши свою: её увидит клиент.",
              ik([cb("📷 Плохо видно условие", A("reject_go", oid, "unclear"))],
                 [cb("🙅 Не наш предмет", A("reject_go", oid, "subject")),
                  cb("⏰ Не успеем", A("reject_go", oid, "late"))],
                 [cb("Без причины", A("reject_go", oid))],
                 [cancel_btn(A("card", oid))]),
              order_id=oid)


async def _do_reject(bot: Bot, session: AsyncSession, config: Config, order_id: int, reason: str | None,
                     admin_id: int) -> Order:
    order = await svc.reject(session, order_id, reason, now_utc(), admin_id)
    if svc.is_any(order):
        await safe_send(bot, order.user_id, texts.ANY_REJECTED.format(
            id=order.id, reason=f"\n\nПричина: {html.escape(reason)}" if reason else ""))
    else:
        await safe_send(bot, order.user_id, texts.ORDER_REJECTED.format(
            id=order.id, reason=f"\n\nПричина: {html.escape(reason)}" if reason else ""))
    admin = await session.get(User, admin_id)
    await mark_order(bot, session, config, order.id, f"🚫 Отклонил(а): {who(admin, admin_id)}")
    return order


@router.message(AdminSG.order_reject)
async def o_reject_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot,
                         config: Config) -> None:
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Напиши причину текстом или выбери кнопку.")
        return
    data = await state.get_data()
    order_id = int(data.get("order_id") or 0)
    await state.clear()
    await close_prompt(bot, data)
    try:
        await _do_reject(bot, session, config, order_id, reason[:1000], message.from_user.id)
    except svc.OrderError as e:
        await message.answer(html.escape(str(e)))
        return
    await answer_card(message, session, order_id, config, "🚫 Заказ отклонён")


@router.callback_query(_cb("reject_go"))
async def o_reject_go(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                      bot: Bot, config: Config) -> None:
    await state.clear()
    try:
        await _do_reject(bot, session, config, callback_data.id, REJECT_REASONS.get(callback_data.v),
                         call.from_user.id)
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("Отклонён")
    await show_card(call, session, callback_data.id, config, "🚫 Заказ отклонён")


# ------------------------------------------------------------------ работа и решение
@router.callback_query(_cb("take"))
async def o_take(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                 config: Config) -> None:
    try:
        order = await svc.take_in_work(session, callback_data.id, now_utc(), call.from_user.id)
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("В работе 🛠")
    await safe_send(bot, order.user_id, texts.ORDER_TAKEN.format(id=order.id))
    admin = await session.get(User, call.from_user.id)
    await mark_order(bot, session, config, order.id, f"🛠 Взял(а) в работу: {who(admin, call.from_user.id)}")
    await show_card(call, session, order.id, config)


def upload_kb(order_id: int, n: int) -> InlineKeyboardMarkup:
    return ik([cb(f"💾 Сохранить ({n})", A("upload_save", order_id))] if n else None,
              [cb(texts.BTN_CANCEL, A("upload_cancel", order_id))])


@router.callback_query(_cb("upload"))
async def o_upload(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                   config: Config) -> None:
    order = await svc.get_order(session, callback_data.id)
    if order is None or order.status not in ("paid", "in_work", "done"):
        await call.answer("Сейчас решение загрузить нельзя: статус изменился.", show_alert=True)
        return
    solutions.reset_staging(config.solutions_dir, order.id)
    await state.set_state(AdminSG.upload_solution)
    await state.update_data(order_id=order.id, files=[])
    await call.answer()
    prompt = UPLOAD_PROMPT.format(id=order.id)
    if svc.is_explain(order):
        prompt = prompt.replace("Решение для заказа", "Файл к объяснению") + \
            "\n<i>Необязательно: объяснение можно просто написать сообщениями.</i>"
    await call.message.answer(prompt, reply_markup=upload_kb(order.id, 0))


@router.message(AdminSG.upload_solution, F.document)
async def o_upload_file(message: Message, state: FSMContext, bot: Bot, config: Config) -> None:
    order_id = int((await state.get_data()).get("order_id") or 0)
    doc = message.document
    name = solutions.safe_name(doc.file_name or "solution.html")
    if Path(name).suffix.lower() not in solutions.ALLOWED_EXT:
        await message.answer("Нужен файл .html, .htm или .zip.")
        return
    if (doc.file_size or 0) > SOLUTION_MAX:
        await message.answer("Файл больше 20 МБ: Telegram не даст боту его скачать. Сожми или раздели на части.")
        return
    dest = solutions.staging_dir(config.solutions_dir, order_id) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await bot.download(doc.file_id, destination=dest)
    except Exception:  # noqa: BLE001
        log.exception("не скачался файл решения")
        await message.answer("Не получилось скачать файл: пришли его ещё раз.")
        return
    admin_id = message.from_user.id
    async with upl.user_lock(admin_id):
        files = [f for f in (await state.get_data()).get("files", []) if f.get("file_name") != name]
        files.append(dict(tg_file_id=doc.file_id, tg_file_unique_id=doc.file_unique_id, file_name=name,
                          mime_type=doc.mime_type, size=doc.file_size))
        await state.update_data(files=files)
    chat_id = message.chat.id

    async def ack() -> None:
        if await state.get_state() != AdminSG.upload_solution.state:
            return
        got = (await state.get_data()).get("files", [])
        names = ", ".join(html.escape(f["file_name"]) for f in got)
        await bot.send_message(chat_id, f"Получено файлов: {len(got)} ({names})", reply_markup=upload_kb(order_id, len(got)))

    upl.schedule_ack(admin_id, "solution", ack)


@router.message(AdminSG.upload_solution)
async def o_upload_other(message: Message) -> None:
    await message.answer("Ждём HTML-файлы или ZIP документом. Когда всё: «💾 Сохранить».")


@router.callback_query(_cb("upload_save"))
async def o_upload_save(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                        bot: Bot, config: Config) -> None:
    data = await state.get_data()
    if await state.get_state() != AdminSG.upload_solution.state or int(data.get("order_id") or 0) != callback_data.id:
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    upl.cancel_ack(call.from_user.id, "solution")
    before = await svc.get_order(session, callback_data.id)
    prev_status = before.status if before else None
    try:
        entry = solutions.finalize(config.solutions_dir, callback_data.id)
    except solutions.SolutionError as e:
        await call.answer(str(e), show_alert=True)
        return
    files = []
    for f in data.get("files", []):
        local = config.solutions_dir / str(callback_data.id) / f["file_name"]
        files.append({**f, "local_path": str(local) if local.exists() else None})
    try:
        order = await svc.attach_solution(session, callback_data.id, entry, files, now_utc(), call.from_user.id)
    except svc.OrderError as e:
        await state.clear()
        await call.answer(str(e), show_alert=True)
        return
    await state.clear()
    await call.answer("Решение сохранено ✅")
    await drop_kb(call)
    if prev_status == "paid":
        await safe_send(bot, order.user_id, texts.ORDER_TAKEN.format(id=order.id))
        admin = await session.get(User, call.from_user.id)
        await mark_order(bot, session, config, order.id, f"🛠 Взял(а) в работу: {who(admin, call.from_user.id)}")
    if order.status == "done":
        explain = svc.is_explain(order)
        text = (f"🔄 Файл к объяснению #{order.id} обновлён." if explain
                else f"🔄 Решение по заказу #{order.id} обновлено.")
        await safe_send(bot, order.user_id, text,
                        reply_markup=explain_done_kb(order, config) if explain else done_kb(order, config))
        if not config.webapp_url:
            await send_solution_files(bot, session, config, order, order.user_id)
    await show_card(call, session, order.id, config, f"✅ Решение сохранено, вход: <code>{html.escape(entry)}</code>",
                    new=True)


@router.callback_query(_cb("upload_cancel"))
async def o_upload_cancel(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                          config: Config) -> None:
    upl.cancel_ack(call.from_user.id, "solution")
    await state.clear()
    shutil.rmtree(solutions.staging_dir(config.solutions_dir, callback_data.id), ignore_errors=True)
    await call.answer("Отменено")
    await drop_kb(call)


@router.callback_query(_cb("done"))
async def o_done(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                 s: RuntimeSettings, bot: Bot, config: Config) -> None:
    await finish_order(call, callback_data.id, state, session, s, bot, config)


async def finish_order(call: CallbackQuery, order_id: int, state: FSMContext, session: AsyncSession,
                       s: RuntimeSettings, bot: Bot, config: Config, *, new: bool = False) -> None:
    """«📦 Готово - отправить клиенту» (из карточки или из чата): выдать заказ, завершить чат админа по нему.
    new - карточку показать новым сообщением (из чата), а у нажатого сообщения снять кнопки."""
    try:
        order, reward = await svc.mark_done(session, order_id, s, now_utc(), call.from_user.id)
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("Отправлено клиенту ✅")
    if svc.is_explain(order):
        await safe_send(bot, order.user_id, texts.EXPLAIN_DONE.format(id=order.id),
                        reply_markup=explain_done_kb(order, config))
        if order.solution_entry and not config.webapp_url:
            await send_solution_files(bot, session, config, order, order.user_id)
        note = "📦 Объяснение выдано: клиент отметит «✅ Всё понятно» или задаст вопрос"
    else:
        await safe_send(bot, order.user_id, texts.ORDER_DONE.format(id=order.id), reply_markup=done_kb(order, config))
        if not config.webapp_url:
            await send_solution_files(bot, session, config, order, order.user_id)
        note = "📦 Решение отправлено клиенту"
    if reward is not None:
        await notify_reward(bot, reward, s)
        referrer = await session.get(User, reward.referrer_id)
        note += f"\n👥 Пригласившему {display_name(referrer)} начислено +{fmt_rub(reward.amount)} бонусами"
        if reward.new_priority:
            note += " · теперь у него «Приоритет»"
        if reward.new_free_month:
            note += " · включён «Месяц бесплатно»"
    admin = await session.get(User, call.from_user.id)
    await mark_order(bot, session, config, order.id, f"📦 Выдал(а) клиенту: {who(admin, call.from_user.id)}")
    chat = await conversation.finish(session, call.from_user.id, conversation.Target(order.user_id, order.id))
    if chat is not None:
        note += "\n" + texts.CHAT_FINISHED.format(mark=texts.CHAT_MARK_ORDER.format(id=order.id), n=chat.sent)
    if new:
        await drop_kb(call)
    await show_card(call, session, order.id, config, note, new=new)


# ------------------------------------------------------------------ переписка по заказу (вся история)
@router.callback_query(_cb("chat"))
async def o_chat(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    """«💬 Переписка»: все сообщения по заказу - клиента и наши, кто и когда (последние, сколько влезет)."""
    oid = callback_data.id
    order = await svc.get_order(session, oid)
    if order is None or order.status == "draft":
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    rows, total = await tickets.order_thread(session, oid, 60)
    lines = [texts.CHAT_HISTORY_TITLE.format(mark=texts.CHAT_MARK_ORDER.format(id=oid), n=total)]
    body = await conversation.history_lines(session, rows, 300)
    while body and len("\n".join(lines + body)) > 3900:
        body.pop(0)
    if not rows:
        lines.append(texts.CHAT_HISTORY_EMPTY)
    elif len(body) < total:
        lines.append(texts.CHAT_HISTORY_SHOWN.format(n=len(body)))
    thread = await session.scalar(select(Ticket).where(Ticket.order_id == oid).order_by(Ticket.id.desc()).limit(1))
    kb = ik([cb(texts.CHAT_BTN, AdminCb(s="c", a="o", id=oid)), cb(texts.CHAT_TO_ORDER_BTN, A("card", oid))],
            [cb(texts.CHAT_CLIENT_MEDIA_BTN, AdminCb(s="t", a="media", id=thread.id))] if thread else None,
            [menu_btn()])
    await call.answer()
    text = "\n".join(lines + body)
    if callback_data.v == "n":  # из подтверждения доставки - новым сообщением, подтверждение не трогаем
        await call.message.answer(text, reply_markup=kb)
    else:
        await show(call, text, kb)


# ------------------------------------------------------------------ спор и возврат
@router.callback_query(_cb("redo"))
async def o_redo(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                 config: Config) -> None:
    try:
        order = await svc.redo(session, callback_data.id, now_utc(), call.from_user.id)
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("Переделываем 🔁")
    await safe_send(bot, order.user_id, texts.REDO.format(id=order.id))
    admin = await session.get(User, call.from_user.id)
    await mark_headers(bot, session, config, "dispute", order.id, f"🔁 На переделку: {who(admin, call.from_user.id)}",
                       markup_for=lambda _lvl: ik([cb("📦 Карточка заказа", A("open", order.id))]))
    await show_card(call, session, order.id, config, "🔁 Заказ снова в работе: загрузи новую версию решения")


@router.callback_query(_cb("refund"), L2)
async def o_refund(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings) -> None:
    order = await svc.get_order(session, callback_data.id)
    if order is None or order.status not in svc.REFUNDABLE:
        await call.answer("Этот заказ нельзя вернуть.", show_alert=True)
        return
    pay = await paysvc.paid_payment_for(session, order.id)
    rub = order.paid_balance + order.paid_invoice
    lines = [f"↩️ <b>Возврат по заказу #{order.id}</b>"]
    lines.append(f"На рублёвый баланс клиента вернётся: <b>{fmt_rub(rub)}</b>" if rub else "Рублями не оплачивался.")
    if order.paid_bonus:
        lines.append(f"Бонусы {fmt_rub(order.paid_bonus)} вернутся новой партией на {s.bonus_ttl_days} дн.")
    if order.free_tasks:
        lines.append(f"Бесплатные задачи «Месяца бесплатно» ({order.free_tasks}) вернутся клиенту.")
    ref = await session.scalar(select(Referral).where(Referral.reward_order_id == order.id,
                                                      Referral.rewarded.is_(True)))
    if ref is not None:
        lines.append(f"👥 Бонус пригласившему ({fmt_rub(ref.reward_amount)}) будет отозван из его остатка.")
    rows = [[cb("↩️ На баланс", A("refund_go", order.id, "balance"))]]
    if pay is not None and pay.provider == "stars" and pay.telegram_payment_charge_id and order.paid_invoice > 0:
        rows[0].append(cb(f"⭐ Звёздами ({pay.amount_stars})", A("refund_go", order.id, "stars")))
        lines.append(f"Или вернуть {pay.amount_stars} ⭐ звёздами (refundStarPayment), остальное на баланс.")
    rows.append([cb(texts.BTN_CANCEL, A("card", order.id))])
    await call.answer()
    await show(call, "\n".join(lines), ik(*rows))


@router.callback_query(_cb("refund_go"), L2)
async def o_refund_go(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings,
                      bot: Bot, config: Config) -> None:
    provider = StarsPaymentProvider()

    async def star_refunder(payment: Payment) -> bool:
        return await provider.refund(bot, payment)

    try:
        res = await paysvc.refund_order(session, callback_data.id, callback_data.v, s, now_utc(), call.from_user.id,
                                        star_refunder=star_refunder)
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    except TelegramBadRequest as e:
        await session.rollback()
        await call.answer(f"Telegram отказал в возврате звёзд: {e.message}"[:200], show_alert=True)
        return
    parts = []
    if res.stars_refunded:
        parts.append(f"{res.stars_refunded} ⭐ звёздами")
    if res.rub_to_balance:
        parts.append(f"{fmt_rub(res.rub_to_balance)} на рублёвый баланс")
    if res.bonus_back:
        parts.append(f"{fmt_rub(res.bonus_back)} бонусами")
    details = ", ".join(parts) or "оплаты не было"
    await safe_send(bot, res.order.user_id, texts.REFUNDED.format(id=res.order.id, details=details))
    note = f"↩️ Возврат: {details}"
    if res.revoke is not None:
        if res.revoke.revoked:
            await safe_send(bot, res.revoke.referrer_id, texts.REF_REVOKED.format(amount=fmt_rub(res.revoke.revoked)))
        note += f"\n👥 У пригласившего отозвано {fmt_rub(res.revoke.revoked)} из {fmt_rub(res.revoke.reward_amount)}"
    admin = await session.get(User, call.from_user.id)
    marker = f"↩️ Возврат сделал(а): {who(admin, call.from_user.id)}"
    await mark_order(bot, session, config, res.order.id, marker)
    await mark_headers(bot, session, config, "dispute", res.order.id, marker,
                       markup_for=lambda _lvl: ik([cb("📦 Карточка заказа", A("open", res.order.id))]))
    await call.answer("Возврат сделан ✅")
    await show_card(call, session, res.order.id, config, note)
