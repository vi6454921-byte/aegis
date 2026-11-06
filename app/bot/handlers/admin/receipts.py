"""Админка → 🧾 Чеки: оплата переводом. Клиент прислал чек - админ сверяет поступление и подтверждает."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb, PayCb
from app.bot.handlers.admin.base import admin_router, menu_btn
from app.bot.keyboards import cb, ik
from app.bot.notify import mark_headers, remember, safe_send, staff_targets
from app.bot.ui import show
from app.config import Config
from app.db.models import Order, Payment, User
from app.services import orders as svc
from app.services import payments as paysvc
from app.services.settings import RuntimeSettings
from app.services.users import display_name, short_name
from app.utils.money import fmt_rub
from app.utils.timeutil import fmt_dt, now_utc, to_local

router = admin_router("admin_receipts")


def P(a: str = "", pid: int = 0, v: str = "") -> AdminCb:  # noqa: N802
    return AdminCb(s="pay", a=a, id=pid, v=v)


def _f(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "pay") & (F.a == action))


def what_of(payment: Payment) -> str:
    return f"заказ #{payment.order_id}" if payment.purpose == "order" else "пополнение баланса"


def receipt_kb(payment: Payment, v: str = "n") -> InlineKeyboardMarkup:
    rows = [[cb("✅ Оплата пришла", P("ok", payment.id, v)), cb("❌ Не пришла", P("no", payment.id, v))]]
    second = [cb("👤 Клиент", AdminCb(s="u", a="open", id=payment.user_id))]
    if payment.order_id:
        second.insert(0, cb(f"📦 Заказ #{payment.order_id}", AdminCb(s="o", a="open", id=payment.order_id)))
    rows.append(second)
    return ik(*rows)


def receipt_header(payment: Payment, user: User | None) -> str:
    text = texts.ADMIN_RECEIPT.format(pid=payment.id, what=what_of(payment), user=display_name(user),
                                      amount=fmt_rub(payment.amount_rub))
    text += f"\nЧек: {'прислан ⬆️' if payment.receipt_file_id else 'без чека'} · {fmt_dt(payment.claimed_at)}"
    return text + "\n<i>Сверь сумму с поступлением на счёт и нажми кнопку.</i>"


async def notify_receipt(bot: Bot, session: AsyncSession, config: Config, payment: Payment, user: User) -> None:
    """Чек от клиента - всем админам: файл чека с подписью и кнопками. Без commit."""
    header = receipt_header(payment, user)
    for admin_id, _lvl in staff_targets(config, 1):
        try:
            if payment.receipt_file_type == "photo":
                msg = await bot.send_photo(admin_id, payment.receipt_file_id, caption=header,
                                           reply_markup=receipt_kb(payment))
            elif payment.receipt_file_type == "document":
                msg = await bot.send_document(admin_id, payment.receipt_file_id, caption=header,
                                              reply_markup=receipt_kb(payment))
            else:
                msg = await bot.send_message(admin_id, header, reply_markup=receipt_kb(payment))
        except Exception:  # noqa: BLE001
            msg = await safe_send(bot, admin_id, header, reply_markup=receipt_kb(payment))
        if msg is not None:
            remember(session, admin_id, msg.message_id, "payment", payment.id, user.id, header)


async def list_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    rows = await paysvc.manual_queue(session)
    lines = [f"🧾 <b>Чеки на проверке</b>: {len(rows)}", "<i>Оплата переводом: сверь поступление и подтверди.</i>"]
    buttons = []
    for p in rows:
        u = await session.get(User, p.user_id)
        buttons.append([cb(f"№{p.id} · {fmt_rub(p.amount_rub)} · {short_name(u)} · {fmt_dt(p.claimed_at)}",
                           P("open", p.id))])
    if not rows:
        lines.append("\nНовых чеков нет 👌")
    return "\n".join(lines), ik(*buttons, [menu_btn()])


async def view(session: AsyncSession, payment: Payment) -> tuple[str, InlineKeyboardMarkup]:
    user = await session.get(User, payment.user_id)
    text = receipt_header(payment, user)
    if payment.order_id:
        order = await session.get(Order, payment.order_id)
        if order is not None:
            text += f"\n\nЗаказ #{order.id}: {texts.STATUS.get(order.status, order.status)} · " \
                    f"цена {fmt_rub(order.price_final or 0)}"
    if payment.reviewed_at:
        text += f"\n\nРешение принято {fmt_dt(payment.reviewed_at)} · статус: {payment.status}"
        return text, ik([cb("⬅️ К чекам", P()), menu_btn()])
    kb = receipt_kb(payment, v="")
    if payment.receipt_file_id:
        kb.inline_keyboard.insert(0, [cb("🧾 Показать чек", P("file", payment.id))])
    kb.inline_keyboard.append([cb("⬅️ К чекам", P()), menu_btn()])
    return text, kb


@router.callback_query(AdminCb.filter((F.s == "pay") & F.a.in_({"", "list"})))
async def r_list(call: CallbackQuery, session: AsyncSession) -> None:
    await call.answer()
    text, kb = await list_view(session)
    await show(call, text, kb)


@router.callback_query(_f("open"))
async def r_open(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    payment = await session.get(Payment, callback_data.id, populate_existing=True)
    if payment is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    text, kb = await view(session, payment)
    await show(call, text, kb)


@router.callback_query(_f("file"))
async def r_file(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    payment = await session.get(Payment, callback_data.id)
    if payment is None or not payment.receipt_file_id:
        await call.answer("Чека нет.", show_alert=True)
        return
    await call.answer()
    caption = f"🧾 Чек по счёту №{payment.id}"
    if payment.receipt_file_type == "photo":
        await bot.send_photo(call.from_user.id, payment.receipt_file_id, caption=caption)
    else:
        await bot.send_document(call.from_user.id, payment.receipt_file_id, caption=caption)


@router.callback_query(_f("ok"))
@router.callback_query(_f("no"))
async def r_confirm_ask(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    payment = await session.get(Payment, callback_data.id, populate_existing=True)
    if payment is None or payment.reviewed_at is not None:
        await call.answer("По этому счёту уже принято решение.", show_alert=True)
        return
    await call.answer()
    user = await session.get(User, payment.user_id)
    amount = fmt_rub(payment.amount_rub)
    if callback_data.a == "ok":
        text = (f"✅ Подтвердить, что <b>{amount}</b> по счёту №{payment.id} ({what_of(payment)}) от "
                f"{display_name(user)} пришли на счёт?\nПосле подтверждения заказ уйдёт в работу.")
        kb = ik([cb("✅ Да, деньги пришли", P("ok_go", payment.id)), cb(texts.BTN_BACK, P("open", payment.id))])
    else:
        text = (f"❌ Поступления <b>{amount}</b> по счёту №{payment.id} нет?\n"
                "Клиенту придёт сообщение: проверить сумму и реквизиты и оплатить снова или написать в /paysupport.")
        kb = ik([cb("❌ Да, не пришли", P("no_go", payment.id)), cb(texts.BTN_BACK, P("open", payment.id))])
    await show(call, text, kb)


@router.callback_query(_f("ok_go"))
@router.callback_query(_f("no_go"))
async def r_decide(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings,
                   bot: Bot, config: Config) -> None:
    from app.bot.handlers.payments import report  # noqa: PLC0415 - handlers.payments импортирует админку

    approved = callback_data.a == "ok_go"
    try:
        payment, res = await paysvc.review_manual(session, callback_data.id, approved, s, now_utc(),
                                                  call.from_user.id)
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    admin = await session.get(User, call.from_user.id)
    stamp = f"{short_name(admin)} · {to_local(now_utc()):%H:%M}"
    if approved:
        await call.answer("Оплата подтверждена ✅")
        await report(bot, config, res, payment.user_id, session, s)
        marker = f"✅ Оплата подтверждена: {stamp}"
    else:
        await call.answer("Отклонено")
        kb = None
        if payment.order_id:
            order = await session.get(Order, payment.order_id)
            if order is not None and order.status in svc.PAYABLE:
                kb = ik([cb(f"{texts.PAY_BTN} · {fmt_rub(order.price_final or 0)}",
                            PayCb(action="order", id=order.id))])
        await safe_send(bot, payment.user_id, texts.MANUAL_REJECTED.format(
            pid=payment.id, amount=fmt_rub(payment.amount_rub)), reply_markup=kb)
        marker = f"❌ Поступление не найдено: {stamp}"
    order_kb = (lambda lvl: ik([cb(f"📦 Заказ #{payment.order_id}", AdminCb(s="o", a="open", id=payment.order_id))])) \
        if payment.order_id else None
    await mark_headers(bot, session, config, "payment", payment.id, marker, markup_for=order_kb)
    text, kb = await view(session, payment)
    await show(call, f"{marker}\n\n{text}", kb)


async def receipt_reminder_text(session: AsyncSession, payments: list[Payment]) -> str:
    lines = ["⏰ <b>Чеки ждут проверки больше часа</b>"]
    for p in payments:
        u = await session.get(User, p.user_id)
        lines.append(f"• №{p.id} · {fmt_rub(p.amount_rub)} · {short_name(u)} · {fmt_dt(p.claimed_at)}")
    return "\n".join(lines)

