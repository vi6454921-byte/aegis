"""Оплаты. Один код зачисления для всех провайдеров: confirm_payment()."""
from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Payment, User
from app.services.admin_log import log_admin
from app.services.ledger import (
    InsufficientFunds,
    available_bonus,
    credit_bonus,
    credit_rub,
    debit_bonus,
    debit_rub,
    lock_user,
)
from app.services.locks import app_lock
from app.services.orders import REFUNDABLE, OrderError
from app.services.pricing import split_payment
from app.services.referrals import RevokeResult, revoke_for_order
from app.utils.money import stars_for

log = logging.getLogger(__name__)


def new_payload() -> str:
    return "ae" + secrets.token_urlsafe(18)


async def find_by_payload(session: AsyncSession, payload: str) -> Payment | None:
    return await session.scalar(select(Payment).where(Payment.payload == payload)
                                .execution_options(populate_existing=True))


async def _cancel_pending(session: AsyncSession, order_id: int) -> None:
    """Старые неоплаченные счета заказа — в отмену. Переводы с присланным чеком не трогаем: их проверяет админ."""
    await session.execute(update(Payment).where(Payment.order_id == order_id, Payment.status == "pending",
                                                Payment.claimed_at.is_(None))
                          .values(status="cancelled"))


async def start_order_payment(session: AsyncSession, order_id: int, user_id: int, s, now: datetime) -> Payment:  # noqa: ANN001
    """Считает разбиение: бонусы (≤ доли цены) → баланс → счёт. Деньги списываются только при подтверждении."""
    async with app_lock():
        order = await session.get(Order, order_id, populate_existing=True)
        if order is None or order.user_id != user_id:
            raise OrderError("Заказ не найден.")
        if order.status != "awaiting_payment" or not order.price_final:
            raise OrderError("Этот заказ сейчас не ждёт оплаты.")
        await _cancel_pending(session, order.id)
        user = await lock_user(session, user_id)
        split = split_payment(order.price_final, await available_bonus(session, user.id, now), user.balance_rub,
                              int(s.bonus_max_share_pct))
        mode = s.payments_mode
        provider = "internal" if split.invoice == 0 else mode
        stars = stars_for(split.invoice, s.rate) if provider == "stars" else None
        payment = Payment(user_id=user.id, order_id=order.id, purpose="order", provider=provider,
                          amount_rub=split.invoice, amount_stars=stars,
                          currency="XTR" if provider == "stars" else "RUB",
                          plan_bonus=split.bonus, plan_balance=split.balance, payload=new_payload(),
                          is_test=(mode == "test"), created_at=now)
        session.add(payment)
        await session.commit()
        return payment


async def start_topup(session: AsyncSession, user_id: int, amount: int, s, now: datetime) -> Payment:  # noqa: ANN001
    if amount not in [int(v) for v in s.topup_packages]:
        raise OrderError("Такого пакета нет.")
    mode = s.payments_mode
    bonus = int(s.topup_bonus_map.get(str(amount), 0)) if s.topup_bonus_enabled else 0
    payment = Payment(user_id=user_id, purpose="topup", provider=mode, amount_rub=amount,
                      amount_stars=stars_for(amount, s.rate) if mode == "stars" else None,
                      currency="XTR" if mode == "stars" else "RUB", topup_bonus=bonus,
                      payload=new_payload(), is_test=(mode == "test"), created_at=now)
    session.add(payment)
    await session.commit()
    return payment


async def check_pre_checkout(session: AsyncSession, payload: str, total_amount: int, currency: str,
                             user_id: int | None = None) -> str | None:
    """None — можно принимать оплату, иначе код ошибки. Отвечать Telegram нужно < 10 с."""
    payment = await find_by_payload(session, payload)
    if payment is None or payment.status != "pending" or payment.provider != "stars":
        return "stale"
    if user_id is not None and payment.user_id != user_id:
        return "user"
    if currency != "XTR" or total_amount != payment.amount_stars:
        return "amount"
    user = await session.get(User, payment.user_id, populate_existing=True)
    if user is not None and user.is_banned:
        return "banned"  # бот для него закрыт — новые оплаты не принимаем
    if payment.purpose == "order":
        order = await session.get(Order, payment.order_id, populate_existing=True)
        if order is None or order.status != "awaiting_payment":
            return "order"
    return None


@dataclass
class ConfirmResult:
    status: str  # order_paid | topup_ok | credited_to_balance | failed | duplicate | not_found
    payment: Payment | None = None
    order: Order | None = None
    user: User | None = None


async def confirm_payment(session: AsyncSession, payment_id: int, s, now: datetime, *,  # noqa: ANN001
                          charge_id: str | None = None, provider_charge_id: str | None = None) -> ConfirmResult:
    """Идемпотентно проводит платёж: pending → paid один раз, charge_id уникален.

    Счёт, отменённый у нас (status=cancelled), но всё-таки оплаченный в Telegram (пришёл charge_id),
    тоже зачисляется — деньги не теряются: на заказ, если он ещё ждёт оплаты, иначе на баланс.
    """
    async with app_lock():
        payment = await session.get(Payment, payment_id, populate_existing=True)
        if payment is None:
            return ConfirmResult("not_found")
        if charge_id and await session.scalar(select(Payment.id).where(
                Payment.telegram_payment_charge_id == charge_id)):
            return ConfirmResult("duplicate", payment)
        if payment.status != "pending" and not (payment.status in ("cancelled", "rejected") and charge_id
                                                and payment.provider not in ("internal", "test")):
            return ConfirmResult("duplicate", payment)
        res = await session.execute(update(Payment).where(Payment.id == payment.id,
                                                          Payment.status == payment.status)
                                    .values(status="paid", paid_at=now, telegram_payment_charge_id=charge_id,
                                            provider_payment_charge_id=provider_charge_id))
        if res.rowcount != 1:
            await session.rollback()
            return ConfirmResult("duplicate", payment)
        await session.refresh(payment)
        user = await lock_user(session, payment.user_id)
        meta = dict(payment_id=payment.id, is_test=payment.is_test)

        if payment.purpose == "topup":
            await credit_rub(session, user, payment.amount_rub, "topup", **meta)
            if payment.topup_bonus > 0:
                await credit_bonus(session, user, payment.topup_bonus, source="topup_bonus", kind="topup_bonus",
                                   ttl_days=int(s.bonus_ttl_days), now=now, **meta)
            await session.commit()
            return ConfirmResult("topup_ok", payment, None, user)

        order = await session.get(Order, payment.order_id, populate_existing=True)
        if payment.amount_rub > 0:
            await credit_rub(session, user, payment.amount_rub, "payment_in", order_id=order.id, **meta)
        need_rub = payment.plan_balance + payment.amount_rub
        ok = (order.status == "awaiting_payment" and order.price_final == payment.plan_bonus + need_rub
              and await available_bonus(session, user.id, now) >= payment.plan_bonus
              and user.balance_rub >= need_rub)
        if not ok:
            if payment.amount_rub == 0:  # денег не пришло (оплата с баланса) — просто отменяем попытку
                payment.status = "cancelled"
                payment.paid_at = None
                await session.commit()
                return ConfirmResult("failed", payment, order, user)
            await session.commit()
            return ConfirmResult("credited_to_balance", payment, order, user)
        try:
            if payment.plan_bonus:
                await debit_bonus(session, user, payment.plan_bonus, kind="order_pay", now=now, order_id=order.id,
                                  **meta)
            if need_rub:
                await debit_rub(session, user, need_rub, "order_pay", order_id=order.id, **meta)
        except InsufficientFunds:  # pragma: no cover — проверено выше
            await session.rollback()
            raise
        order.paid_bonus = payment.plan_bonus
        order.paid_balance = payment.plan_balance
        order.paid_invoice = payment.amount_rub
        order.status = "paid"
        order.paid_at = now
        order.is_test = payment.is_test
        await session.commit()
        return ConfirmResult("order_paid", payment, order, user)


@dataclass
class RefundResult:
    order: Order
    method: str
    rub_to_balance: int
    stars_refunded: int
    bonus_back: int
    revoke: RevokeResult | None


StarRefunder = Callable[[Payment], Awaitable[bool]]


async def paid_payment_for(session: AsyncSession, order_id: int) -> Payment | None:
    return await session.scalar(select(Payment).where(Payment.order_id == order_id, Payment.status == "paid")
                                .order_by(Payment.id.desc()).limit(1).execution_options(populate_existing=True))


async def refund_order(session: AsyncSession, order_id: int, method: str, s, now: datetime,  # noqa: ANN001
                       admin_id: int | None, star_refunder: StarRefunder | None = None) -> RefundResult:
    """Возврат: рубли — на баланс (или звёзды — через refundStarPayment), бонусы — новой партией."""
    async with app_lock():
        order = await session.get(Order, order_id, populate_existing=True)
        if order is None or order.status not in REFUNDABLE:
            raise OrderError("Этот заказ нельзя вернуть.")
        payment = await paid_payment_for(session, order.id)
        stars_refunded = 0
        if method == "stars":
            if payment is None or payment.provider != "stars" or not payment.telegram_payment_charge_id \
                    or order.paid_invoice <= 0 or star_refunder is None:
                raise OrderError("Звёздами вернуть нельзя — этот заказ оплачен не звёздами.")
            if not await star_refunder(payment):
                raise OrderError("Telegram не подтвердил возврат звёзд.")
            stars_refunded = payment.amount_stars or 0
        elif method != "balance":
            raise OrderError("Неизвестный способ возврата.")
        user = await lock_user(session, order.user_id)
        meta = dict(order_id=order.id, payment_id=payment.id if payment else None, admin_id=admin_id,
                    is_test=order.is_test)
        rub_back = order.paid_balance + order.paid_invoice
        if rub_back:
            await credit_rub(session, user, rub_back, "refund", **meta)
        if method == "stars":
            await debit_rub(session, user, order.paid_invoice, "stars_refund", **meta)
        if order.paid_bonus:
            await credit_bonus(session, user, order.paid_bonus, source="refund", kind="refund",
                               ttl_days=int(s.bonus_ttl_days), now=now, **meta)
        if payment is not None:
            payment.status = "refunded"
            payment.refunded_at = now
            payment.refund_method = method
        if order.free_tasks and user.free_month_until and user.free_month_until > now:
            user.free_month_tasks_left += order.free_tasks
        order.status = "refunded"
        order.closed_at = now
        revoke = await revoke_for_order(session, order, now)
        log_admin(session, admin_id, "refund", "order", order.id, method=method, rub=rub_back,
                  bonus=order.paid_bonus, stars=stars_refunded)
        await session.commit()
        return RefundResult(order, method, rub_back - (order.paid_invoice if method == "stars" else 0),
                            stars_refunded, order.paid_bonus, revoke)


async def admin_adjust(session: AsyncSession, user_id: int, account: str, amount: int, comment: str, s,  # noqa: ANN001
                       now: datetime, admin_id: int) -> User:
    """Начислить (+) или списать (−) рубли/бонусы с комментарием. Баланс не уходит в минус."""
    async with app_lock():
        user = await lock_user(session, user_id)
        meta = dict(admin_id=admin_id, comment=comment)
        if account == "rub":
            if amount > 0:
                await credit_rub(session, user, amount, "admin_credit", **meta)
            else:
                await debit_rub(session, user, -amount, "admin_debit", **meta)
        else:
            if amount > 0:
                await credit_bonus(session, user, amount, source="admin", kind="admin_credit",
                                   ttl_days=int(s.bonus_ttl_days), now=now, **meta)
            else:
                await debit_bonus(session, user, -amount, kind="admin_debit", now=now, **meta)
        log_admin(session, admin_id, "adjust_balance", "user", user.id, account=account, amount=amount,
                  comment=comment)
        await session.commit()
        return user


# ------------------------------------------------------------------ оплата переводом (ручная проверка)
MANUAL_OPEN = ("pending", "cancelled")


async def claim_manual(session: AsyncSession, payment_id: int, user_id: int, file_id: str | None,
                       file_type: str | None, now: datetime) -> Payment:
    """Клиент нажал «Я оплатил» и прислал чек (или без чека). Коммитит."""
    async with app_lock():
        payment = await session.get(Payment, payment_id, populate_existing=True)
        if payment is None or payment.user_id != user_id or payment.provider != "manual":
            raise OrderError("Счёт не найден.")
        if payment.status not in MANUAL_OPEN:
            raise OrderError("Этот счёт уже закрыт.")
        if payment.claimed_at is not None:
            raise OrderError("already")
        payment.claimed_at = now
        payment.receipt_file_id, payment.receipt_file_type = file_id, file_type
        if payment.status == "cancelled":
            payment.status = "pending"
        await session.commit()
        return payment


async def review_manual(session: AsyncSession, payment_id: int, approved: bool, s, now: datetime,  # noqa: ANN001
                        admin_id: int) -> tuple[Payment, ConfirmResult | None]:
    """Админ сверил поступление: approved — провести платёж, иначе отклонить. Повторное решение — ошибка."""
    payment = await session.get(Payment, payment_id, populate_existing=True)
    if payment is None or payment.provider != "manual":
        raise OrderError("Счёт не найден.")
    if payment.reviewed_at is not None or payment.status in ("paid", "refunded"):
        raise OrderError("По этому счёту уже принято решение.")
    if not approved:
        payment.status = "rejected"
        payment.reviewed_by, payment.reviewed_at = admin_id, now
        log_admin(session, admin_id, "manual_reject", "payment", payment.id, amount=payment.amount_rub)
        await session.commit()
        return payment, None
    res = await confirm_payment(session, payment.id, s, now, charge_id=f"manual-{payment.id}")
    payment = await session.get(Payment, payment_id, populate_existing=True)
    payment.reviewed_by, payment.reviewed_at = admin_id, now
    log_admin(session, admin_id, "manual_confirm", "payment", payment.id, amount=payment.amount_rub,
              result=res.status)
    await session.commit()
    return payment, res


async def manual_queue(session: AsyncSession, limit: int = 30) -> list[Payment]:
    return list((await session.execute(select(Payment).where(
        Payment.provider == "manual", Payment.claimed_at.is_not(None), Payment.reviewed_at.is_(None),
        Payment.status.in_(MANUAL_OPEN)).order_by(Payment.claimed_at).limit(limit))).scalars())
