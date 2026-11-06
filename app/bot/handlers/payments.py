"""Оплата заказов и пополнение баланса: тестовые деньги, Telegram Stars, перевод с чеком, баланс/бонусы."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb, PayCb
from app.bot.keyboards import cb, ik
from app.bot.notify import notify_admins, safe_send
from app.bot.render import subject_name, tasks_word
from app.bot.states import PaySG
from app.bot.ui import drop_kb
from app.config import Config
from app.db.models import Order, Payment, User
from app.payments import get_provider
from app.payments.manual_provider import ManualPaymentProvider
from app.services import design as dsvc
from app.services import orders as svc
from app.services import payments as paysvc
from app.services.settings import RuntimeSettings
from app.services.users import display_name
from app.utils.money import fmt_rub
from app.utils.timeutil import now_utc

log = logging.getLogger(__name__)
router = Router(name="payments")


def plan_text(bonus: int, balance: int, invoice: int) -> str:
    return texts.PAY_PLAN.format(bonus=fmt_rub(bonus), balance=fmt_rub(balance), invoice=fmt_rub(invoice))


def order_invoice_texts(order: Order, payment: Payment) -> tuple[str, str]:
    title = f"Аегис · заказ #{order.id}"
    if order.subject == svc.EXPLAIN:
        desc = "Объяснение материала по фото лекции или практики: простыми словами, по шагам."
    elif order.subject == svc.ANY:
        desc = texts.ANY_INVOICE_DESC
    elif dsvc.is_design(order):
        desc = texts.DESIGN_INVOICE_DESC.format(what=dsvc.describe_plain(order))
    else:
        desc = (f"{subject_name(order.subject).strip()}, {tasks_word(order.task_count or 0)}: разбор с 3D-моделью, "
                f"решением и алгоритмом.")
    if payment.plan_bonus or payment.plan_balance:
        desc += f" Цена {fmt_rub(order.price_final or 0)}, бонусами {fmt_rub(payment.plan_bonus)}, " \
                f"с баланса {fmt_rub(payment.plan_balance)}."
    return title, desc


async def send_invoice(bot: Bot, payment: Payment, title: str, desc: str, s: RuntimeSettings) -> None:
    if payment.provider == "manual":
        await ManualPaymentProvider(str(s.manual_pay_details or "")).create_invoice(bot, payment, title, desc)
        return
    await get_provider(payment.provider).create_invoice(bot, payment, title, desc)


async def report(bot: Bot, config: Config, res: paysvc.ConfirmResult, chat_id: int,
                 session: AsyncSession | None = None, s: RuntimeSettings | None = None) -> None:
    """Сообщения пользователю и админу по итогу проведения платежа (общие для всех провайдеров).
    v2.4: заказ «🎨 Файлы и дизайн» сразу уходит исполнителям (нужна session, чтобы запомнить уведомления)."""
    if res.status == "order_paid" and session is not None and dsvc.is_design(res.order):
        from app.bot import designs  # noqa: PLC0415

        o = res.order
        await designs.notify_paid(bot, session, config, o, res.user,
                                  plan_text(o.paid_bonus, o.paid_balance, o.paid_invoice),
                                  autosend_files=bool(s.admin_autosend_files) if s is not None else True)
    elif res.status == "order_paid":
        o = res.order
        await safe_send(bot, chat_id, texts.PAY_DONE_ORDER.format(id=o.id))
        await notify_admins(bot, config, texts.ADMIN_PAID.format(
            id=o.id, user=display_name(res.user), price=fmt_rub(o.price_final or 0),
            plan=plan_text(o.paid_bonus, o.paid_balance, o.paid_invoice), test=" 🧪 тест" if o.is_test else ""),
            reply_markup=ik([cb("📦 Открыть заказ", AdminCb(s="o", a="open", id=o.id))]))
    elif res.status == "topup_ok":
        p = res.payment
        bonus = f" и +{fmt_rub(p.topup_bonus)} бонусами" if p.topup_bonus else ""
        await safe_send(bot, chat_id, texts.PAY_DONE_TOPUP.format(amount=fmt_rub(p.amount_rub), bonus=bonus))
        await notify_admins(bot, config, texts.ADMIN_TOPUP.format(
            user=display_name(res.user), amount=fmt_rub(p.amount_rub), test=" 🧪 тест" if p.is_test else ""))
    elif res.status == "credited_to_balance":
        p = res.payment
        await safe_send(bot, chat_id, texts.PAY_TO_BALANCE.format(id=p.order_id, amount=fmt_rub(p.amount_rub)))
        await notify_admins(bot, config,
                            f"💰 Платёж {fmt_rub(p.amount_rub)} от {display_name(res.user)} по заказу #{p.order_id} "
                            f"зачислен на баланс: заказ уже не ждал оплаты или сумма изменилась"
                            + (" 🧪 тест" if p.is_test else ""))
    elif res.status == "failed":
        await safe_send(bot, chat_id, "Не получилось: баланс или бонусы изменились. Открой заказ и нажми "
                                      "«💳 Оплатить» ещё раз: мы пересчитаем.")
    elif res.status == "duplicate":
        await safe_send(bot, chat_id, "Этот платёж уже учтён ✅")
    else:
        await safe_send(bot, chat_id, texts.PAY_STALE)


# ------------------------------------------------------------------ оплата заказа
@router.callback_query(PayCb.filter(F.action == "order"))
async def pay_order(call: CallbackQuery, callback_data: PayCb, session: AsyncSession, db_user: User,
                    s: RuntimeSettings, bot: Bot) -> None:
    try:
        payment = await paysvc.start_order_payment(session, callback_data.id, db_user.id, s, now_utc())
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer()
    await show_payment(call.message, session, payment, s, bot)


async def show_payment(message: Message, session: AsyncSession, payment: Payment, s: RuntimeSettings,
                       bot: Bot) -> None:
    """Хватает баланса и бонусов - экран «✅ Оплатить с баланса», иначе счёт (тест, звёзды или перевод)."""
    order = await svc.get_order(session, payment.order_id)
    plan = plan_text(payment.plan_bonus, payment.plan_balance, payment.amount_rub)
    if payment.provider == "internal":
        await message.answer(
            f"🧾 Заказ #{order.id}: <b>{fmt_rub(order.price_final)}</b>\n{plan}\nСчёт не нужен: хватает баланса.",
            reply_markup=ik([cb(texts.PAY_BALANCE_BTN, PayCb(action="balance", id=payment.id))],
                            [cb(texts.BTN_CANCEL, PayCb(action="drop", id=payment.id))]))
        return
    if payment.plan_bonus or payment.plan_balance:
        await message.answer(f"🧾 Заказ #{order.id}: <b>{fmt_rub(order.price_final)}</b>\n{plan}")
    title, desc = order_invoice_texts(order, payment)
    await send_invoice(bot, payment, title, desc, s)


async def _own_pending(session: AsyncSession, payment_id: int, user: User) -> Payment | None:
    payment = await session.get(Payment, payment_id, populate_existing=True)
    if payment is None or payment.user_id != user.id or payment.status != "pending":
        return None
    return payment


@router.callback_query(PayCb.filter(F.action == "balance"))
async def pay_from_balance(call: CallbackQuery, callback_data: PayCb, session: AsyncSession, db_user: User,
                           s: RuntimeSettings, bot: Bot, config: Config) -> None:
    payment = await _own_pending(session, callback_data.id, db_user)
    if payment is None or payment.provider != "internal":
        await call.answer(texts.PAY_STALE, show_alert=True)
        return
    if payment.is_test and s.payments_mode != "test":  # посчитано в тестовом режиме - тест уже выключен
        payment.status = "cancelled"
        await session.commit()
        await call.answer(texts.PAY_TEST_DISABLED, show_alert=True)
        await drop_kb(call)
        return
    await call.answer()
    await drop_kb(call)
    res = await paysvc.confirm_payment(session, payment.id, s, now_utc())
    await report(bot, config, res, call.from_user.id, session, s)


@router.callback_query(PayCb.filter(F.action == "test"))
async def pay_test(call: CallbackQuery, callback_data: PayCb, session: AsyncSession, db_user: User,
                   s: RuntimeSettings, bot: Bot, config: Config) -> None:
    payment = await _own_pending(session, callback_data.id, db_user)
    if payment is None or payment.provider != "test":
        await call.answer(texts.PAY_STALE, show_alert=True)
        await drop_kb(call)  # счёт уже не действует - убираем его кнопки из чата
        return
    if s.payments_mode != "test":  # тест выключен - счёт больше не нужен
        payment.status = "cancelled"
        await session.commit()
        await call.answer(texts.PAY_TEST_DISABLED, show_alert=True)
        await drop_kb(call)
        return
    await call.answer("🧪 Оплачено тестовыми деньгами")
    await drop_kb(call)
    res = await get_provider("test").handle_success(session, payment, s, now_utc(),
                                                    charge_id=f"test_{payment.payload}")
    await report(bot, config, res, call.from_user.id, session, s)


@router.callback_query(PayCb.filter(F.action == "drop"))
async def pay_drop(call: CallbackQuery, callback_data: PayCb, session: AsyncSession, db_user: User) -> None:
    payment = await _own_pending(session, callback_data.id, db_user)
    if payment is not None and payment.provider in ("test", "internal", "manual") and payment.claimed_at is None:
        payment.status = "cancelled"
        await session.commit()
    await call.answer("Счёт отменён")
    await drop_kb(call)


# ------------------------------------------------------------------ пополнение
@router.callback_query(PayCb.filter(F.action == "topup"))
async def pay_topup(call: CallbackQuery, callback_data: PayCb, session: AsyncSession, db_user: User,
                    s: RuntimeSettings, bot: Bot) -> None:
    try:
        payment = await paysvc.start_topup(session, db_user.id, callback_data.id, s, now_utc())
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer()
    desc = f"Пополнение рублёвого баланса Аегис на {fmt_rub(payment.amount_rub)}."
    if payment.topup_bonus:
        desc += f" Бонус к пополнению: +{fmt_rub(payment.topup_bonus)} бонусами."
    await send_invoice(bot, payment, "Аегис · пополнение", desc, s)


# ------------------------------------------------------------------ Telegram Stars
@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, session: AsyncSession) -> None:
    error = await paysvc.check_pre_checkout(session, query.invoice_payload, query.total_amount, query.currency,
                                            user_id=query.from_user.id)
    if error is None:
        await query.answer(ok=True)
    else:
        log.info("pre_checkout отклонён (%s): %s", error, query.invoice_payload)
        await query.answer(ok=False, error_message=texts.PRECHECKOUT_FAIL)


@router.message(F.successful_payment)
async def successful_payment(message: Message, session: AsyncSession, db_user: User, s: RuntimeSettings,
                             bot: Bot, config: Config) -> None:
    sp = message.successful_payment
    payment = await paysvc.find_by_payload(session, sp.invoice_payload)
    if payment is None:
        log.error("successful_payment без счёта: %s %s %s", sp.invoice_payload, sp.total_amount,
                  sp.telegram_payment_charge_id)
        await notify_admins(bot, config, f"⚠️ Пришла оплата без счёта в БД: {sp.total_amount} {sp.currency} от "
                                         f"{display_name(db_user)}, charge_id <code>{sp.telegram_payment_charge_id}"
                                         f"</code>. Разберись вручную (возврат: refundStarPayment).")
        await message.answer("Платёж получен, но счёт не нашёлся. Мы уже в курсе и разберёмся: "
                             "деньги не потеряются. Вопросы: /paysupport.")
        return
    res = await get_provider("stars").handle_success(session, payment, s, now_utc(),
                                                     charge_id=sp.telegram_payment_charge_id,
                                                     provider_charge_id=sp.provider_payment_charge_id)
    await report(bot, config, res, message.chat.id, session, s)


# ------------------------------------------------------------------ перевод по реквизитам: «Я оплатил» + чек
@router.callback_query(PayCb.filter(F.action == "claim"))
async def pay_claim(call: CallbackQuery, callback_data: PayCb, state: FSMContext, session: AsyncSession,
                    db_user: User) -> None:
    payment = await session.get(Payment, callback_data.id, populate_existing=True)
    if payment is None or payment.user_id != db_user.id or payment.provider != "manual" \
            or payment.status not in paysvc.MANUAL_OPEN:
        await call.answer(texts.PAY_STALE, show_alert=True)
        return
    if payment.claimed_at is not None:
        await call.answer(texts.MANUAL_ALREADY_CLAIMED, show_alert=True)
        return
    await call.answer()
    await state.set_state(PaySG.receipt)
    await state.update_data(pid=payment.id)
    await call.message.answer(texts.MANUAL_RECEIPT_PROMPT.format(pid=payment.id, amount=fmt_rub(payment.amount_rub)),
                              reply_markup=ik([cb(texts.MANUAL_NO_RECEIPT_BTN, PayCb(action="noreceipt", id=payment.id))],
                                              [cb(texts.BTN_CANCEL, PayCb(action="claim_cancel", id=payment.id))]))


async def _claim(message: Message, session: AsyncSession, user: User, bot: Bot, config: Config, pid: int,
                 file_id: str | None, file_type: str | None) -> None:
    from app.bot.handlers.admin.receipts import notify_receipt  # noqa: PLC0415 - админка импортирует этот модуль

    try:
        payment = await paysvc.claim_manual(session, pid, user.id, file_id, file_type, now_utc())
    except svc.OrderError as e:
        await message.answer(texts.MANUAL_ALREADY_CLAIMED if str(e) == "already" else str(e))
        return
    await notify_receipt(bot, session, config, payment, user)
    await session.commit()
    await message.answer(texts.MANUAL_CLAIMED)


@router.message(PaySG.receipt, F.photo | F.document)
async def pay_receipt(message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot,
                      config: Config) -> None:
    pid = int((await state.get_data()).get("pid") or 0)
    await state.clear()
    if message.photo:
        await _claim(message, session, db_user, bot, config, pid, message.photo[-1].file_id, "photo")
    else:
        await _claim(message, session, db_user, bot, config, pid, message.document.file_id, "document")


@router.message(PaySG.receipt)
async def pay_receipt_other(message: Message) -> None:
    await message.answer("Пришли скрин или фото чека (можно PDF) или нажми «Отправить без чека».")


@router.callback_query(PayCb.filter(F.action == "noreceipt"))
async def pay_no_receipt(call: CallbackQuery, callback_data: PayCb, state: FSMContext, session: AsyncSession,
                         db_user: User, bot: Bot, config: Config) -> None:
    await state.clear()
    await call.answer()
    await drop_kb(call)
    await _claim(call.message, session, db_user, bot, config, callback_data.id, None, None)


@router.callback_query(PayCb.filter(F.action == "claim_cancel"))
async def pay_claim_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer(texts.CANCELLED)
    await drop_kb(call)
