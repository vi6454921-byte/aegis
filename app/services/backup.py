"""Бэкапы SQLite через backup API (а не копированием открытого файла)."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from app.config import Config, sqlite_file
from app.utils.timeutil import now_utc, to_local

PREFIX = "aegis-"


def backup_sqlite(db_path: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(dest)


def rotate(backups_dir: Path, keep: int) -> list[Path]:
    files = sorted(backups_dir.glob(f"{PREFIX}*.db"), key=lambda p: p.name, reverse=True)
    removed = files[keep:]
    for p in removed:
        p.unlink(missing_ok=True)
    return removed


async def make_backup(config: Config) -> Path:
    db = sqlite_file(config.database_url)
    if db is None:
        raise RuntimeError("Бэкап из бота работает только для SQLite. Для PostgreSQL используй pg_dump.")
    dest = config.backups_dir / f"{PREFIX}{to_local(now_utc()):%Y%m%d-%H%M%S}.db"
    await asyncio.to_thread(backup_sqlite, db, dest)
    rotate(config.backups_dir, config.backup_keep)
    return dest
