"""Лимит мест: не больше max_tasks_per_day новых задач в день (по дате отправки, Самара).

Заказы по безлимиту (промокод) считаются в лимите как обычные. Если включена настройка
«Безлимит обходит дневной лимит», безлимитные заказы принимаются сверх лимита и места у других не занимают
(ручное закрытие приёма действует на всех)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order
from app.utils.timeutil import local_today

NOT_COUNTED = ("draft", "cancelled", "rejected")


@dataclass(frozen=True)
class IntakeState:
    open: bool
    reason: str | None  # manual | limit | None
    remaining: int | None  # None - без лимита (открыто вручную)
    show_remaining: bool
    limit: int
    used: int


async def tasks_used_on(session: AsyncSession, day: date, exclude_unlimited: bool = False) -> int:
    q = select(func.coalesce(func.sum(Order.task_count), 0)).where(
        Order.slot_date == day, Order.status.not_in(NOT_COUNTED))
    if exclude_unlimited:
        q = q.where(Order.unlimited.is_(False))
    return int((await session.execute(q)).scalar_one())


async def intake_state(session: AsyncSession, s, now: datetime, unlimited: bool = False) -> IntakeState:  # noqa: ANN001
    """unlimited - заказ от клиента с безлимитом (важно, только если включён обход лимита)."""
    bypass = bool(s.unlimited_bypass_limit)
    used = await tasks_used_on(session, local_today(now), exclude_unlimited=bypass)
    limit = int(s.max_tasks_per_day)
    mode = s.intake_mode
    if mode == "closed":
        return IntakeState(False, "manual", 0, False, limit, used)
    if mode == "open" or (unlimited and bypass):
        return IntakeState(True, None, None, False, limit, used)
    remaining = max(limit - used, 0)
    return IntakeState(remaining > 0, None if remaining > 0 else "limit", remaining,
                       remaining <= int(s.show_slots_threshold), limit, used)
