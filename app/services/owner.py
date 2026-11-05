"""Первый администратор без ручной правки .env.

Если ADMIN_IDS пуст, при запуске бот печатает в консоль одноразовый код. Владелец отправляет боту
«/admin КОД» — и становится админом. Его id сохраняется в базе (таблица settings) и, если рядом лежит
.env, вписывается в строку ADMIN_IDS. Когда ADMIN_IDS в .env задан, сохранённый в базе не используется.
Код живёт до перезапуска; после MAX_FAILURES неверных попыток выдаётся новый (перебор бесполезен).
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.db.models import Setting

log = logging.getLogger(__name__)

SETTING_KEY = "owner_admin_ids"
ENV_FILE = Path(".env")  # тот же файл, что читает Config (относительно папки запуска)
MAX_FAILURES = 10


def banner(code: str, username: str) -> str:
    line = "=" * 66
    return (f"\n{line}\n"
            f"  Администратор ещё не назначен. Чтобы им стать, откройте бота\n"
            f"  https://t.me/{username} и отправьте ему сообщение:\n\n"
            f"      /admin {code}\n"
            f"{line}")


class OwnerClaim:
    """Одноразовый код «стать админом». Один на диспетчер: dp["owner_claim"]."""

    def __init__(self, username: str) -> None:
        self.username = username
        self.code: str | None = None
        self.failures = 0

    @property
    def active(self) -> bool:
        return self.code is not None

    def start(self) -> str:
        old, code = self.code, self.code
        while code == old:
            code = str(secrets.randbelow(900_000) + 100_000)
        self.code, self.failures = code, 0
        return code

    def check(self, attempt: str | None) -> bool:
        """True — код верный (и больше не действует). Пустая попытка ошибкой не считается."""
        if self.code is None or not attempt or not attempt.strip():
            return False
        if secrets.compare_digest(attempt.strip().encode(), self.code.encode()):
            self.code = None
            return True
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            log.warning("много неверных попыток /admin КОД — старый код больше не действует, новый:%s",
                        banner(self.start(), self.username))
        return False


def ids_text(ids: set[int]) -> str:
    return ",".join(map(str, sorted(ids)))


async def load_saved(session: AsyncSession) -> set[int]:
    row = await session.get(Setting, SETTING_KEY)
    if row is None:
        return set()
    try:
        return {int(x) for x in json.loads(row.value)}
    except (TypeError, ValueError):
        return set()


async def restore_saved(session: AsyncSession, config: Config) -> set[int]:
    """При пустом ADMIN_IDS — подхватить админа, назначенного кодом в прошлый раз."""
    if config.admin_ids:
        return set()
    saved = await load_saved(session)
    if saved:
        config.admin_ids_raw = ids_text(saved)
    return saved


async def grant(session: AsyncSession, config: Config, user_id: int) -> None:
    ids = config.admin_ids | {user_id}
    config.admin_ids_raw = ids_text(ids)  # проверки доступа читают config.admin_ids — действует сразу
    row = await session.get(Setting, SETTING_KEY)
    raw = json.dumps(sorted(ids))
    if row is None:
        session.add(Setting(key=SETTING_KEY, value=raw))
    else:
        row.value = raw
    await session.commit()
    if save_to_env(ids):
        log.info("ADMIN_IDS=%s записан в %s", ids_text(ids), ENV_FILE)


def save_to_env(ids: set[int], path: Path | None = None) -> bool:
    """Вписать ADMIN_IDS в .env (если файл есть — в Docker его внутри контейнера нет, там хватает базы)."""
    path = ENV_FILE if path is None else path
    if not path.is_file():
        return False
    line = "ADMIN_IDS=" + ids_text(ids)
    try:
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(r"(?m)^[ \t]*ADMIN_IDS[ \t]*=.*$", line, text, count=1)
        if not n:
            new = text.rstrip("\r\n") + "\n" + line + "\n"
        path.write_text(new, encoding="utf-8")
    except OSError as e:
        log.warning("не удалось записать ADMIN_IDS в %s (%s) — админ сохранён в базе", path, e)
        return False
    return True
