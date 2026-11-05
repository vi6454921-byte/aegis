"""Применение миграций при старте: alembic upgrade head. Никаких drop_all/create_all."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

ROOT = Path(__file__).resolve().parents[2]


def alembic_config(database_url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["skip_logging"] = True
    return cfg


def upgrade_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")
