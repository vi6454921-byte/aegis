"""Режим оплаты: 🧪 тест ↔ настоящие деньги (⭐ Telegram Stars или 🏦 перевод). Переключает админ 2 уровня.

При включении настоящей оплаты тестовая исчезает полностью:
• все новые счета — в звёздах (или переводом), кнопки «💳 Оплатить (тест)» больше не появляются;
• неоплаченные тестовые счета отменяются — их старые кнопки перестают работать;
• тестовые деньги, что ещё лежат на балансах (рубли и бонусы из тестовых оплат), списываются —
  ими нельзя заплатить за настоящий заказ. Настоящие рубли и бонусы не трогаются.
Тестовые заказы и история остаются с пометкой 🧪 — удалить их можно в «💾 Данные → 🧹 Очистить тестовые данные».
Вернуть тест можно в любой момент — уже выставленные счета в звёздах при этом остаются действительными.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BonusLot, Order, Payment, Transaction, User
from app.db.sums import grouped
from app.services.admin_log import log_admin
from app.services.ledger import debit_rub, lock_user
from app.services.locks import app_lock
from app.services.settings import REAL_PAYMENT_MODES, set_setting

MODES = ("test", *REAL_PAYMENT_MODES)
TEST_OFF = "test_off"  # вид транзакции: тестовые деньги убраны при включении настоящей оплаты


@dataclass
class TestLeftovers:
    """Что осталось от тестового режима и уберётся при включении настоящей оплаты."""

    invoices: int = 0  # неоплаченных тестовых счетов
    rub: int = 0  # тестовых рублей на балансах, копейки
    bonus: int = 0  # тестовых бонусов
    users: int = 0  # у скольких клиентов есть тестовые деньги
    orders_open: int = 0  # тестовых заказов в очереди / в работе (остаются, просто помечены 🧪)

    @property
    def money(self) -> bool:
        return bool(self.invoices or self.rub or self.bonus)


def _pending_test_payments():  # noqa: ANN202
    return (Payment.status == "pending") & Payment.is_test.is_(True)


async def _test_rub_by_user(session: AsyncSession) -> dict[int, int]:
    """Сколько тестовых рублей у каждого ещё на балансе: тестовые начисления минус тестовые траты,
    но не больше самого баланса — настоящие деньги не трогаем."""
    sums = await grouped(session, Transaction.user_id, Transaction.amount,
                         Transaction.account == "rub", Transaction.is_test.is_(True))
    return {uid: amount for uid, amount in sums.items() if amount > 0}


def _live_test_lots():  # noqa: ANN202
    return select(BonusLot).where(BonusLot.is_test.is_(True), BonusLot.remaining > 0, BonusLot.expired.is_(False))


async def leftovers(session: AsyncSession) -> TestLeftovers:
    """Подсчёт для экрана подтверждения — ничего не меняет."""
    res = TestLeftovers()
    res.invoices = int(await session.scalar(select(func.count(Payment.id)).where(_pending_test_payments())) or 0)
    users: set[int] = set()
    for uid, amount in (await _test_rub_by_user(session)).items():
        balance = int(await session.scalar(select(User.balance_rub).where(User.id == uid)) or 0)
        if min(amount, balance) > 0:
            res.rub += min(amount, balance)
            users.add(uid)
    for lot in (await session.execute(_live_test_lots())).scalars():
        res.bonus += lot.remaining
        users.add(lot.user_id)
    res.users = len(users)
    res.orders_open = int(await session.scalar(select(func.count(Order.id)).where(
        Order.is_test.is_(True), Order.status.in_(("paid", "in_work")))) or 0)
    return res


async def _remove_test_money(session: AsyncSession, now: datetime) -> TestLeftovers:
    """Отменяет неоплаченные тестовые счета и списывает тестовые деньги. Под app_lock, без commit."""
    res = TestLeftovers()
    cancelled = await session.execute(update(Payment).where(_pending_test_payments()).values(status="cancelled"))
    res.invoices = int(cancelled.rowcount or 0)
    users: set[int] = set()
    for uid, amount in (await _test_rub_by_user(session)).items():
        user = await lock_user(session, uid)
        take = min(amount, user.balance_rub)
        if take > 0:
            await debit_rub(session, user, take, TEST_OFF, is_test=True,
                            comment="тестовые деньги убраны: включена настоящая оплата")
            res.rub += take
            users.add(uid)
    for lot in list((await session.execute(_live_test_lots().order_by(BonusLot.id))).scalars()):
        user = await lock_user(session, lot.user_id)
        user.balance_bonus -= lot.remaining
        session.add(Transaction(user_id=user.id, account="bonus", amount=-lot.remaining, kind=TEST_OFF,
                                bonus_lot_id=lot.id, is_test=True, created_at=now,
                                comment="тестовые бонусы убраны: включена настоящая оплата"))
        res.bonus += lot.remaining
        users.add(user.id)
        lot.remaining = 0
        lot.expired = True
    res.users = len(users)
    return res


async def switch_mode(session: AsyncSession, old_mode: str, new_mode: str, admin_id: int,
                      now: datetime) -> TestLeftovers:
    """Переключает режим оплаты. В настоящий режим — вместе с уборкой тестовых денег. Коммитит."""
    if new_mode not in MODES:
        raise ValueError(new_mode)
    async with app_lock():
        removed = await _remove_test_money(session, now) if new_mode in REAL_PAYMENT_MODES else TestLeftovers()
        await set_setting(session, "payments_mode", new_mode)
        log_admin(session, admin_id, "payments_mode", "setting", "payments_mode", old=old_mode, new=new_mode,
                  cancelled_invoices=removed.invoices, removed_rub=removed.rub, removed_bonus=removed.bonus)
        await session.commit()
    return removed


async def tidy_on_start(session: AsyncSession, mode: str, now: datetime) -> TestLeftovers:
    """При запуске в настоящем режиме убирает остатки теста — например, если режим сменили в .env или
    переключили в старой версии бота, где тестовые деньги оставались на балансах. Коммитит."""
    if mode not in REAL_PAYMENT_MODES:
        return TestLeftovers()
    async with app_lock():
        removed = await _remove_test_money(session, now)
        if removed.money:
            log_admin(session, None, "test_money_removed", "setting", "payments_mode", mode=mode,
                      cancelled_invoices=removed.invoices, removed_rub=removed.rub, removed_bonus=removed.bonus)
            await session.commit()
        else:
            await session.rollback()
    return removed
