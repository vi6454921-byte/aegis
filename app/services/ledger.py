"""Журнал денег. Любое изменение баланса - строка в transactions + обновление кэша в users,
в одной транзакции БД. Commit делает вызывающий сервис."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import actionlog
from app.db.models import BonusLot, Transaction, User
from app.utils.timeutil import end_of_local_day, to_local


class InsufficientFunds(Exception):
    pass


async def lock_user(session: AsyncSession, user_id: int) -> User:
    res = await session.execute(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    return res.scalar_one()


def _tx(user: User, account: str, amount: int, kind: str, **meta) -> Transaction:
    return Transaction(user_id=user.id, account=account, amount=amount, kind=kind, **meta)


async def credit_rub(session: AsyncSession, user: User, amount: int, kind: str, **meta) -> Transaction:
    if amount <= 0:
        raise ValueError("amount must be positive")
    user.balance_rub += amount
    tx = _tx(user, "rub", amount, kind, **meta)
    session.add(tx)
    actionlog.money(user, "rub", amount, kind)
    return tx


async def debit_rub(session: AsyncSession, user: User, amount: int, kind: str, **meta) -> Transaction:
    if amount <= 0:
        raise ValueError("amount must be positive")
    if user.balance_rub < amount:
        raise InsufficientFunds
    user.balance_rub -= amount
    tx = _tx(user, "rub", -amount, kind, **meta)
    session.add(tx)
    actionlog.money(user, "rub", -amount, kind)
    return tx


def lot_expiry(now: datetime, ttl_days: int) -> datetime:
    """Бонус живёт ttl_days дней - до 23:59 последнего дня (Самара)."""
    return end_of_local_day(to_local(now + timedelta(days=int(ttl_days))).date())


async def credit_bonus(session: AsyncSession, user: User, amount: int, *, source: str, kind: str,
                       ttl_days: int, now: datetime, referral_id: int | None = None,
                       order_id: int | None = None, payment_id: int | None = None,
                       admin_id: int | None = None, comment: str | None = None,
                       is_test: bool = False) -> BonusLot:
    if amount <= 0:
        raise ValueError("amount must be positive")
    lot = BonusLot(user_id=user.id, amount=amount, remaining=amount, source=source,
                   expires_at=lot_expiry(now, ttl_days), referral_id=referral_id, order_id=order_id,
                   is_test=is_test, created_at=now)
    session.add(lot)
    await session.flush()
    user.balance_bonus += amount
    session.add(_tx(user, "bonus", amount, kind, bonus_lot_id=lot.id, order_id=order_id,
                    payment_id=payment_id, admin_id=admin_id, comment=comment, is_test=is_test))
    actionlog.money(user, "bonus", amount, source)
    return lot


async def active_lots(session: AsyncSession, user_id: int, now: datetime) -> list[BonusLot]:
    res = await session.execute(
        select(BonusLot)
        .where(BonusLot.user_id == user_id, BonusLot.remaining > 0, BonusLot.expired.is_(False),
               BonusLot.expires_at > now)
        .order_by(BonusLot.expires_at, BonusLot.id)
        .execution_options(populate_existing=True)
    )
    return list(res.scalars().all())


async def available_bonus(session: AsyncSession, user_id: int, now: datetime) -> int:
    res = await session.execute(
        select(func.coalesce(func.sum(BonusLot.remaining), 0)).where(
            BonusLot.user_id == user_id, BonusLot.remaining > 0, BonusLot.expired.is_(False),
            BonusLot.expires_at > now)
    )
    return int(res.scalar_one())


async def debit_bonus(session: AsyncSession, user: User, amount: int, *, kind: str, now: datetime,
                      allow_partial: bool = False, prefer_lot_id: int | None = None,
                      order_id: int | None = None, payment_id: int | None = None,
                      admin_id: int | None = None, comment: str | None = None,
                      is_test: bool = False) -> int:
    """Списывает бонусы, начиная с самых старых партий. Возвращает списанную сумму."""
    if amount <= 0:
        return 0
    lots = await active_lots(session, user.id, now)
    if prefer_lot_id is not None:
        lots.sort(key=lambda lot: lot.id != prefer_lot_id)
    total = sum(lot.remaining for lot in lots)
    if total < amount and not allow_partial:
        raise InsufficientFunds
    left = take_total = min(amount, total)
    for lot in lots:
        if left <= 0:
            break
        take = min(lot.remaining, left)
        lot.remaining -= take
        left -= take
        session.add(_tx(user, "bonus", -take, kind, bonus_lot_id=lot.id, order_id=order_id,
                        payment_id=payment_id, admin_id=admin_id, comment=comment, is_test=is_test))
    user.balance_bonus -= take_total
    actionlog.money(user, "bonus", -take_total, kind)
    return take_total


async def expire_due_lots(session: AsyncSession, now: datetime) -> dict[int, int]:
    """Сжигает просроченные партии. Возвращает {user_id: сгорело}."""
    res = await session.execute(
        select(BonusLot).where(BonusLot.expired.is_(False), BonusLot.expires_at <= now)
        .order_by(BonusLot.user_id, BonusLot.id)
    )
    burned: dict[int, int] = {}
    users: dict[int, User] = {}
    for lot in res.scalars().all():
        if lot.remaining > 0:
            user = users.get(lot.user_id) or await lock_user(session, lot.user_id)
            users[lot.user_id] = user
            user.balance_bonus -= lot.remaining
            session.add(_tx(user, "bonus", -lot.remaining, "expire", bonus_lot_id=lot.id, is_test=lot.is_test))
            burned[lot.user_id] = burned.get(lot.user_id, 0) + lot.remaining
            lot.remaining = 0
        lot.expired = True
    return burned
