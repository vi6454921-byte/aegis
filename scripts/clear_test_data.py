"""Удаляет все тестовые данные (заказы, оплаты, транзакции, бонусы с пометкой is_test) и пересчитывает балансы.

    python scripts/clear_test_data.py          # спросит подтверждение
    python scripts/clear_test_data.py --yes
То же самое есть в боте: /admin → 💾 Данные → «Очистить тестовые».
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import _bootstrap  # noqa: F401

from app.config import get_config
from app.db.engine import make_engine, make_sessionmaker
from app.services import settings
from app.services.backup import make_backup
from app.services.maintenance import clear_test_data, integrity_issues, test_data_counts
from app.utils.timeutil import now_utc


def _confirm() -> bool:
    answer = input("Удалить? Перед удалением будет сделан бэкап. [y/N] ")
    return answer.strip().lower() in ("y", "yes", "д", "да")


async def run(yes: bool) -> int:
    config = get_config()
    settings.configure(config)
    engine = make_engine(config.database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            counts = await test_data_counts(session)
            print("Тестовые данные:", ", ".join(f"{k}: {v}" for k, v in counts.items()))
            if not any(counts.values()):
                print("Удалять нечего.")
                return 0
            if not yes and not await asyncio.to_thread(_confirm):
                print("Отменено.")
                return 1
            try:
                print("Бэкап:", await make_backup(config))
            except RuntimeError as e:
                print("Бэкап пропущен:", e)
            s = await settings.load_settings(session)
            removed = await clear_test_data(session, config, s, None, now_utc())
            print("Удалено:", ", ".join(f"{k}: {v}" for k, v in removed.items()))
            issues = await integrity_issues(session)
            print("Целостность:", "✅ всё сходится" if not issues else f"❌ {len(issues)} расхождений")
            return 0 if not issues else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    sys.exit(asyncio.run(run(parser.parse_args().yes)))
