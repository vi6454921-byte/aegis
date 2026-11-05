"""Денежные суммы считаем в Python, а не через SQL SUM().

SQLite прерывает SUM() ошибкой «integer overflow», как только промежуточная сумма выходит за 64 бита.
Баланс одного пользователя может доходить до предела BIGINT (выдача денег админом), поэтому сумма
по нескольким балансам или операциям в SQL уронила бы статистику и проверку целостности.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def total(session: AsyncSession, column, *where) -> int:  # noqa: ANN001
    """Сумма столбца по строкам, подходящим под условия (пусто — 0)."""
    q = select(column)
    if where:
        q = q.where(*where)
    return sum(int(v or 0) for v in (await session.execute(q)).scalars())


async def grouped(session: AsyncSession, key, column, *where) -> dict:  # noqa: ANN001
    """{значение key: сумма column} — аналог GROUP BY key с SUM(column)."""
    q = select(key, column)
    if where:
        q = q.where(*where)
    out: dict = defaultdict(int)
    for k, v in (await session.execute(q)).all():
        out[k] += int(v or 0)
    return dict(out)
