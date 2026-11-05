"""Проверка целостности: сумма транзакций = кэш балансов, остатки бонусных партий = журнал.

    python scripts/check_integrity.py
    docker compose exec bot python scripts/check_integrity.py
Код выхода 0 — всё сходится, 1 — есть расхождения.
"""
from __future__ import annotations

import asyncio
import sys

import _bootstrap  # noqa: F401

from app.config import get_config
from app.db.engine import make_engine, make_sessionmaker
from app.services.maintenance import integrity_issues, ledger_totals
from app.utils.money import fmt_rub


async def run() -> int:
    config = get_config()
    engine = make_engine(config.database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            issues = await integrity_issues(session)
            tx, rub, bonus = await ledger_totals(session)
    finally:
        await engine.dispose()
    print(f"Транзакций: {tx}; на балансах: {fmt_rub(rub)} рублями и {fmt_rub(bonus)} бонусами.")
    if not issues:
        print("✅ Всё сходится.")
        return 0
    print(f"❌ Расхождений: {len(issues)}")
    for line in issues:
        print(" -", line)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
