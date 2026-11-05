"""Восстановление БД из бэкапа. ПЕРЕД ЗАПУСКОМ ОСТАНОВИ БОТА.

    python scripts/restore_backup.py                 # показать бэкапы
    python scripts/restore_backup.py latest          # восстановить последний
    python scripts/restore_backup.py data/backups/aegis-20250101-040000.db

В Docker:
    docker compose stop bot
    docker compose run --rm bot python scripts/restore_backup.py latest
    docker compose start bot

Текущая БД сначала копируется в data/backups/before-restore-*.db — откатиться можно тем же скриптом.
После запуска бот сам применит миграции, если бэкап сделан на старой версии.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from app.config import get_config, sqlite_file
from app.services.backup import PREFIX, backup_sqlite
from app.utils.timeutil import now_utc, to_local


def check_backup(path: Path) -> str | None:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "файл повреждён (integrity_check не ok)"
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return f"это не база SQLite ({e})"
    if not {"users", "orders", "transactions", "alembic_version"} <= tables:
        return "в файле нет таблиц бота"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backup", nargs="?", help="путь к файлу бэкапа или latest")
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    args = parser.parse_args()

    config = get_config()
    db = sqlite_file(config.database_url)
    if db is None:
        print("Скрипт работает только с SQLite. Для PostgreSQL используй pg_dump/pg_restore.")
        return 1
    backups = sorted(config.backups_dir.glob(f"{PREFIX}*.db"), key=lambda p: p.name)
    if not args.backup:
        if not backups:
            print(f"В {config.backups_dir} бэкапов нет.")
            return 1
        print("Бэкапы (последний — внизу):")
        for p in backups:
            print(f"  {p}  ({p.stat().st_size // 1024} КБ)")
        print("\nВосстановить: python scripts/restore_backup.py latest  (или путь к файлу)")
        return 0

    src = (backups[-1] if backups else None) if args.backup == "latest" else Path(args.backup)
    if src is None or not src.is_file():
        print("Файл бэкапа не найден.")
        return 1
    problem = check_backup(src)
    if problem:
        print(f"Не восстанавливаю: {problem}.")
        return 1
    print(f"Бэкап: {src}\nТекущая БД: {db}")
    answer = "y" if args.yes else input("Бот остановлен? Заменить текущую БД бэкапом? [y/N] ")
    if answer.strip().lower() not in ("y", "yes", "д", "да"):
        print("Отменено.")
        return 1

    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        safety = config.backups_dir / f"before-restore-{to_local(now_utc()):%Y%m%d-%H%M%S}.db"
        backup_sqlite(db, safety)
        print(f"Текущая БД сохранена: {safety}")
    for suffix in ("-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    tmp = db.with_name(db.name + ".restore")
    shutil.copyfile(src, tmp)
    os.replace(tmp, db)
    print("✅ Восстановлено. Запусти бота — миграции применятся автоматически.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
