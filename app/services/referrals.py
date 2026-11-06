"""Рефералка: +бонус пригласившему, когда первый оплаченный деньгами заказ друга выдан."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BonusLot, Order, Referral
from app.services.ledger import available_bonus, credit_bonus, debit_bonus, lock_user
from app.services.users import paid_friends


@dataclass
class RewardResult:
    referrer_id: int
    amount: int
    expires_at: datetime | None
    paid_friends: int
    new_priority: bool
    new_free_month: bool
    free_until: datetime | None


@dataclass
class RevokeResult:
    referrer_id: int
    revoked: int
    reward_amount: int


async def reward_for_delivered(session: AsyncSession, order: Order, s, now: datetime) -> RewardResult | None:  # noqa: ANN001
    """Вызывается под app_lock, без commit."""
    if order.rub_paid <= 0:  # заплачено только бонусами/бесплатно - не считается
        return None
    ref = await session.scalar(select(Referral).where(Referral.referred_id == order.user_id))
    if ref is None or ref.rewarded:
        return None
    referrer = await lock_user(session, ref.referrer_id)
    amount = int(s.referral_bonus)
    lot = None
    if amount > 0:
        lot = await credit_bonus(session, referrer, amount, source="referral", kind="referral",
                                 ttl_days=int(s.bonus_ttl_days), now=now, referral_id=ref.id,
                                 order_id=order.id, is_test=order.is_test)
    ref.rewarded = True
    ref.reward_order_id = order.id
    ref.reward_amount = amount
    ref.reward_is_test = order.is_test
    ref.rewarded_at = now
    await session.flush()
    n = await paid_friends(session, referrer.id)
    new_priority = new_free = False
    if n >= int(s.priority_threshold) and not referrer.is_priority:
        referrer.is_priority = True
        referrer.priority_auto = True
        new_priority = True
    if n >= int(s.free_month_threshold) and not referrer.free_month_auto:
        referrer.free_month_auto = True
        referrer.free_month_until = now + timedelta(days=int(s.free_month_days))
        referrer.free_month_tasks_left = int(s.free_month_tasks)
        new_free = True
    return RewardResult(referrer.id, amount, lot.expires_at if lot else None, n, new_priority, new_free,
                        referrer.free_month_until if new_free else None)


async def revoke_for_order(session: AsyncSession, order: Order, now: datetime) -> RevokeResult | None:
    """Если вернули заказ, за который дали бонус, - отзываем из остатка бонусов (не ниже нуля)."""
    ref = await session.scalar(select(Referral).where(
        Referral.referred_id == order.user_id, Referral.rewarded.is_(True), Referral.reward_order_id == order.id))
    if ref is None:
        return None
    referrer = await lock_user(session, ref.referrer_id)
    lot = await session.scalar(select(BonusLot).where(BonusLot.referral_id == ref.id, BonusLot.source == "referral")
                               .order_by(BonusLot.id.desc()).limit(1))
    amount = min(ref.reward_amount, await available_bonus(session, referrer.id, now))
    revoked = 0
    if amount > 0:
        revoked = await debit_bonus(session, referrer, amount, kind="referral_revoke", now=now, allow_partial=True,
                                    prefer_lot_id=lot.id if lot else None, order_id=order.id,
                                    is_test=ref.reward_is_test)
    reward_amount = ref.reward_amount
    ref.rewarded = False
    ref.reward_order_id = None
    ref.rewarded_at = None
    ref.reward_amount = 0
    ref.revoked_count += 1
    return RevokeResult(referrer.id, revoked, reward_amount)
