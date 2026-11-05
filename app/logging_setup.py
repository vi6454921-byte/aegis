"""Логи: stdout + data/logs/aegis.log с ротацией (5 МБ × 5).

Токен бота в логах маскируется: как есть, в URL-кодировке (`:` → `%3A`) и любой похожий на токен
Telegram (`123456789:AA…`) — на случай, если в лог попадёт чужой или старый токен."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

from app.config import Config

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MASK = "***"
# id бота (6–12 цифр) + «:» (или %3A) + секретная часть из 30+ символов
TOKEN_RE = re.compile(r"(?<!\d)\d{6,12}(?::|%3[Aa])[A-Za-z0-9_-]{30,}")


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, MASK)
    return TOKEN_RE.sub(MASK, text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__(FORMAT)
        variants: list[str] = []
        for secret in secrets:
            if not secret:
                continue
            for variant in (secret, quote(secret, safe=""), quote(secret)):
                if variant not in variants:
                    variants.append(variant)
        self.secrets = sorted(variants, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


def setup_logging(config: Config) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.log_level.strip().upper(), logging.INFO)
    formatter = RedactingFormatter([config.bot_token])
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)
    stream = logging.StreamHandler(sys.stdout)
    file = RotatingFileHandler(config.logs_dir / "aegis.log", maxBytes=5 * 1024 * 1024, backupCount=5,
                               encoding="utf-8")
    for handler in (stream, file):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    for noisy in ("aiogram.event", "apscheduler", "aiohttp.access", "sqlalchemy.engine", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
