"""Проверка целостности денег и очистка тестовых данных."""
from __future__ import annotations

import shutil
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.db.models import AdminMessage, BonusLot, Order, OrderFile, Payment, Referral, Ticket, Transaction, User
from app.db.sums import grouped, total
from app.services.admin_log import log_admin
from app.services.locks import app_lock
from app.services.users import paid_friends
from app.utils.money import fmt_rub


async def integrity_issues(session: AsyncSession) -> list[str]:
    """Сумма транзакций = кэш балансов, остатки партий = журнал, ничего не ушло в минус."""
    rub = await grouped(session, Transaction.user_id, Transaction.amount, Transaction.account == "rub")
    bonus = await grouped(session, Transaction.user_id, Transaction.amount, Transaction.account == "bonus")
    lots = await grouped(session, BonusLot.user_id, BonusLot.remaining)
    per_lot = await grouped(session, Transaction.bonus_lot_id, Transaction.amount,
                            Transaction.bonus_lot_id.is_not(None))
    issues: list[str] = []
    for user in (await session.execute(select(User))).scalars():
        uid, cash_rub, cash_bonus = user.id, user.balance_rub, user.balance_bonus
        if cash_rub != rub.get(uid, 0):
            issues.append(f"id {uid}: рубли в кэше {fmt_rub(cash_rub)}, по журналу {fmt_rub(rub.get(uid, 0))}")
        if cash_bonus != bonus.get(uid, 0):
            issues.append(f"id {uid}: бонусы в кэше {fmt_rub(cash_bonus)}, по журналу {fmt_rub(bonus.get(uid, 0))}")
        if cash_bonus != lots.get(uid, 0):
            issues.append(f"id {uid}: бонусы в кэше {fmt_rub(cash_bonus)}, в партиях {fmt_rub(lots.get(uid, 0))}")
        if cash_rub < 0 or cash_bonus < 0:
            issues.append(f"id {uid}: отрицательный баланс")
    for lot in (await session.execute(select(BonusLot))).scalars():
        journal = per_lot.get(lot.id, 0)
        if lot.remaining != journal:
            issues.append(f"партия #{lot.id}: остаток {fmt_rub(lot.remaining)}, по журналу {fmt_rub(journal)}")
        if lot.remaining < 0 or lot.remaining > lot.amount:
            issues.append(f"партия #{lot.id}: остаток вне диапазона")
    return issues


async def ledger_totals(session: AsyncSession) -> tuple[int, int, int]:
    tx = int(await session.scalar(select(func.count(Transaction.id))) or 0)
    rub = await total(session, User.balance_rub)
    bonus = await total(session, User.balance_bonus)
    return tx, rub, bonus


async def test_data_counts(session: AsyncSession) -> dict[str, int]:
    return {
        "orders": int(await session.scalar(select(func.count(Order.id)).where(Order.is_test.is_(True))) or 0),
        "payments": int(await session.scalar(select(func.count(Payment.id)).where(Payment.is_test.is_(True))) or 0),
        "transactions": int(await session.scalar(select(func.count(Transaction.id))
                                                 .where(Transaction.is_test.is_(True))) or 0),
        "lots": int(await session.scalar(select(func.count(BonusLot.id)).where(BonusLot.is_test.is_(True))) or 0),
    }


async def clear_test_data(session: AsyncSession, config: Config, s, admin_id: int | None,  # noqa: ANN001
                          now: datetime) -> dict[str, int]:
    """Удаляет всё тестовое (is_test) и пересчитывает кэши из журнала."""
    async with app_lock():
        test_orders = select(Order.id).where(Order.is_test.is_(True))
        order_ids = list((await session.execute(test_orders)).scalars())
        test_payments = select(Payment.id).where(or_(Payment.is_test.is_(True), Payment.order_id.in_(test_orders)))
        test_lots = select(BonusLot.id).where(or_(BonusLot.is_test.is_(True), BonusLot.order_id.in_(test_orders)))
        counts = await test_data_counts(session)
        await session.execute(delete(Transaction).where(or_(
            Transaction.is_test.is_(True), Transaction.order_id.in_(test_orders),
            Transaction.payment_id.in_(test_payments), Transaction.bonus_lot_id.in_(test_lots))))
        await session.execute(update(Referral).where(or_(Referral.reward_is_test.is_(True),
                                                         Referral.reward_order_id.in_(test_orders)))
                              .values(rewarded=False, reward_order_id=None, reward_amount=0,
                                      reward_is_test=False, rewarded_at=None))
        await session.execute(delete(BonusLot).where(BonusLot.id.in_(test_lots)))
        await session.execute(delete(AdminMessage).where(AdminMessage.kind == "payment",
                                                         AdminMessage.ref_id.in_(test_payments)))
        await session.execute(delete(Payment).where(Payment.id.in_(test_payments)))
        await session.execute(delete(OrderFile).where(OrderFile.order_id.in_(order_ids)))
        # переписка по тестовым заказам остаётся в обращениях, но без ссылки на удаляемый заказ
        await session.execute(update(Ticket).where(Ticket.order_id.in_(order_ids)).values(order_id=None))
        await session.execute(delete(AdminMessage).where(AdminMessage.kind.in_(("order", "dispute")),
                                                         AdminMessage.ref_id.in_(order_ids)))
        await session.execute(delete(Order).where(Order.id.in_(order_ids)))

        per_lot = await grouped(session, Transaction.bonus_lot_id, Transaction.amount,
                                Transaction.bonus_lot_id.is_not(None))
        for lot in (await session.execute(select(BonusLot))).scalars():
            lot.remaining = max(per_lot.get(lot.id, 0), 0)
        rub = await grouped(session, Transaction.user_id, Transaction.amount, Transaction.account == "rub")
        bonus = await grouped(session, Transaction.user_id, Transaction.amount, Transaction.account == "bonus")
        users = list((await session.execute(select(User).execution_options(populate_existing=True))).scalars())
        await session.flush()
        for user in users:
            user.balance_rub = rub.get(user.id, 0)
            user.balance_bonus = bonus.get(user.id, 0)
            if user.priority_auto or user.free_month_auto:
                n = await paid_friends(session, user.id)
                if user.priority_auto and n < int(s.priority_threshold):
                    user.is_priority = user.priority_auto = False
                if user.free_month_auto and n < int(s.free_month_threshold):
                    user.free_month_auto = False
                    user.free_month_until = None
                    user.free_month_tasks_left = 0
        log_admin(session, admin_id, "clear_test_data", None, None, **counts)
        await session.commit()
    for oid in order_ids:
        shutil.rmtree(config.uploads_dir / str(oid), ignore_errors=True)
        shutil.rmtree(config.solutions_dir / str(oid), ignore_errors=True)
    return counts
