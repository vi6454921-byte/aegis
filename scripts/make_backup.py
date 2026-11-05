"""Бэкап БД прямо сейчас (SQLite backup API, бот можно не останавливать).

    python scripts/make_backup.py
    docker compose exec bot python scripts/make_backup.py
"""
from __future__ import annotations

import asyncio

import _bootstrap  # noqa: F401

from app.config import get_config
from app.services.backup import make_backup


def main() -> None:
    config = get_config()
    config.ensure_dirs()
    path = asyncio.run(make_backup(config))
    print(f"Готово: {path} ({path.stat().st_size // 1024} КБ). Хранится последних: {config.backup_keep}.")


if __name__ == "__main__":
    main()
