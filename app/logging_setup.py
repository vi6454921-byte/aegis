"""Логи: stdout + data/logs/aegis.log с ротацией (5 МБ × 5). Время везде по Самаре.

v2.4: журнал действий (логгер «aegis.actions», app/actionlog.py) пишется коротко:
«2026-11-05 19:32:08 | @username (id 123) | нажал «💰 Баланс»». Шум aiogram о связи с Telegram
(Request timeout, повторные подключения) понижен до одной строки WARNING без трейсбека.

Токен бота в логах маскируется: как есть, в URL-кодировке (`:` → `%3A`) и любой похожий на токен
Telegram (`123456789:AA…`), на случай, если в лог попадёт чужой или старый токен."""
from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

from app.config import Config
from app.utils.timeutil import TZ

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
ACTIONS = "aegis.actions"
ACTION_FORMAT = "%(asctime)s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
MASK = "***"
# id бота (6-12 цифр) + «:» (или %3A) + секретная часть из 30+ символов
TOKEN_RE = re.compile(r"(?<!\d)\d{6,12}(?::|%3[Aa])[A-Za-z0-9_-]{30,}")


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, MASK)
    return TOKEN_RE.sub(MASK, text)


def samara_time(secs: float | None) -> time.struct_time:
    return datetime.fromtimestamp(secs if secs is not None else time.time(), TZ).timetuple()


class RedactingFormatter(logging.Formatter):
    """Обычные строки: «время уровень логгер: текст»; журнал действий: «время | кто | что»."""

    converter = staticmethod(samara_time)

    def __init__(self, secrets: list[str]):
        super().__init__(FORMAT, DATEFMT)
        self.actions = logging.Formatter(ACTION_FORMAT, DATEFMT)
        self.actions.converter = samara_time
        variants: list[str] = []
        for secret in secrets:
            if not secret:
                continue
            for variant in (secret, quote(secret, safe=""), quote(secret)):
                if variant not in variants:
                    variants.append(variant)
        self.secrets = sorted(variants, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        if record.name == ACTIONS:
            return redact(self.actions.format(record), self.secrets)
        return redact(super().format(record), self.secrets)


class NetworkNoiseFilter(logging.Filter):
    """aiogram при обрыве связи пишет ERROR «Failed to fetch updates» и WARNING «Sleep for … seconds»
    на каждую попытку. Оставляем одну строку WARNING без трейсбека, «Sleep for» убираем."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)
        if msg.startswith("Sleep for"):
            return False
        if msg.startswith("Failed to fetch updates"):
            error = f"{record.args[0]}: {record.args[1]}" if isinstance(record.args, tuple) \
                and len(record.args) >= 2 else record.getMessage()
            record.msg, record.args = "нет связи с Telegram (%s), повторяем подключение", (error,)
        elif msg.startswith("Connection established"):
            record.msg, record.args = "связь с Telegram восстановлена", ()
        else:
            return True
        if record.levelno > logging.WARNING:
            record.levelno, record.levelname = logging.WARNING, "WARNING"
        record.exc_info = record.exc_text = record.stack_info = None
        return True


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
    logging.getLogger(ACTIONS).setLevel(logging.INFO)  # журнал действий виден даже при LOG_LEVEL=WARNING
    dispatcher = logging.getLogger("aiogram.dispatcher")
    for flt in list(dispatcher.filters):
        if isinstance(flt, NetworkNoiseFilter):
            dispatcher.removeFilter(flt)
    dispatcher.addFilter(NetworkNoiseFilter())
