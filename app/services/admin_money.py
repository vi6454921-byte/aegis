"""Выдача и списание денег админом 2 уровня (v2.2): обычные рубли на балансе (account "rub").

Через журнал (ledger) под app_lock, с записью в admin_log. Повторное нажатие «Подтвердить» не начисляет дважды:
у каждой операции одноразовый код op — если он уже есть в журнале админа, операция не повторяется.
Списание не уводит баланс ниже нуля: списывается не больше, чем есть. Сумма — любая, лишь бы влезла в BIGINT.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminLog, User
from app.services.admin_log import log_admin
from app.services.ledger import credit_rub, debit_rub, lock_user
from app.services.locks import app_lock
from app.utils.money import INT64_MAX, fits_int64

ACTION = "give_money"


class GiveError(Exception):
    """Текст для админа."""


@dataclass
class GiveResult:
    applied: int  # фактически: + начислено, − списано (копейки)
    before: int
    after: int
    clamped: bool = False  # списали меньше запрошенного — на балансе было меньше
    nothing: bool = False  # баланс 0 — списывать нечего
    duplicate: bool = False  # эта операция уже выполнена (повторное нажатие)


def new_op() -> str:
    return secrets.token_hex(8)  # только 0-9a-f: безопасно для LIKE


def check_amount(amount: int, balance: int) -> bool:
    """Сумма и итоговый баланс помещаются в 64-битное целое (копейки)."""
    return fits_int64(amount) and abs(amount) <= INT64_MAX and (amount < 0 or fits_int64(balance + amount))


async def _done_before(session: AsyncSession, admin_id: int, op: str) -> bool:
    return bool(await session.scalar(select(AdminLog.id).where(
        AdminLog.admin_id == admin_id, AdminLog.action == ACTION, AdminLog.details.like(f'%"op": "{op}"%')
    ).limit(1)))


async def give(session: AsyncSession, user_id: int, amount: int, comment: str | None, admin_id: int,
               op: str, now: datetime) -> GiveResult:
    """amount > 0 — выдать, < 0 — списать. Коммитит."""
    if amount == 0:
        raise GiveError("Сумма не может быть нулевой.")
    comment = (comment or "").strip()[:500] or None
    async with app_lock():
        user: User = await lock_user(session, user_id)
        before = user.balance_rub
        if await _done_before(session, admin_id, op):
            return GiveResult(0, before, before, duplicate=True)
        if not check_amount(amount, before):
            raise GiveError("overflow")
        meta = dict(admin_id=admin_id, comment=comment)
        if amount > 0:
            await credit_rub(session, user, amount, "admin_topup", **meta)
            applied = amount
        else:
            take = min(-amount, before)
            if take <= 0:
                return GiveResult(0, before, before, nothing=True)
            await debit_rub(session, user, take, "admin_withdraw", **meta)
            applied = -take
        log_admin(session, admin_id, ACTION, "user", user.id, amount=applied, requested=amount, comment=comment,
                  before=before, after=user.balance_rub, op=op)
        await session.commit()
        return GiveResult(applied, before, user.balance_rub, clamped=applied != amount)
