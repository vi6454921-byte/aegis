"""Настройки процесса из .env (pydantic-settings).

Здесь только то, что нужно для запуска. Всё, что админ меняет на лету
(цены, лимиты, тексты), хранится в таблице settings - см. app/services/settings.py.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")
    bot_username: str = Field(default="", alias="BOT_USERNAME")
    database_url: str = Field(default="sqlite+aiosqlite:///data/aegis.db", alias="DATABASE_URL")
    data_dir: Path = Field(default=Path("data"), alias="DATA_DIR")
    webapp_base_url: str = Field(default="", alias="WEBAPP_BASE_URL")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8080, alias="WEB_PORT")
    payments_mode: str = Field(default="test", alias="PAYMENTS_MODE")
    rub_per_star: Decimal = Field(default=Decimal("1.75"), alias="RUB_PER_STAR")
    telegram_test_env: bool = Field(default=False, alias="TELEGRAM_TEST_ENV")
    max_tasks_per_day: int = Field(default=15, alias="MAX_TASKS_PER_DAY")
    show_slots_threshold: int = Field(default=5, alias="SHOW_SLOTS_THRESHOLD")
    stats_offset_done: int = Field(default=90, alias="STATS_OFFSET_DONE")
    stats_offset_first_try: int = Field(default=84, alias="STATS_OFFSET_FIRST_TRY")
    support_contact: str = Field(default="", alias="SUPPORT_CONTACT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    backup_hour: int = Field(default=4, alias="BACKUP_HOUR")
    backup_keep: int = Field(default=14, alias="BACKUP_KEEP")
    # Закреплённые админы 2 уровня (v2.2): ники без @ через запятую, регистр не важен. Ник привязывается к
    # Telegram id при первом сообщении боту - дальше права только по id (см. app/services/pinned.py).
    senior_admin_usernames_raw: str = Field(default="badVInq,dimaslavikov1", alias="SENIOR_ADMIN_USERNAMES")
    # Необязательно: id закреплённых админов числом - тогда привязка по нику не нужна.
    senior_admin_ids_raw: str = Field(default="", alias="SENIOR_ADMIN_IDS")

    # Админы, назначенные в боте (таблица admins): {user_id: уровень}. Кэш процесса - см. app/services/roles.py
    _staff: dict[int, int] = PrivateAttr(default_factory=dict)
    _staff_loaded: bool = PrivateAttr(default=False)
    # Закреплённые админы: id с admins.pinned и привязки ников {ник: (id, состояние)}, где состояние -
    # active (действует), owner (снят владельцем в боте) или env (ник убрали из .env)
    _pinned: set[int] = PrivateAttr(default_factory=set)
    _pinned_bind: dict[str, tuple[int, str]] = PrivateAttr(default_factory=dict)
    _spoof_alerted: set[tuple[str, int]] = PrivateAttr(default_factory=set)

    @property
    def admin_ids(self) -> set[int]:
        """Владельцы (ADMIN_IDS или назначенные кодом /admin): всегда админы 2-го уровня."""
        ids: set[int] = set()
        for part in self.admin_ids_raw.replace(";", ",").replace(" ", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
        return ids

    @property
    def senior_usernames(self) -> dict[str, str]:
        """Ники закреплённых админов из .env: {ник в нижнем регистре: как записан}."""
        result: dict[str, str] = {}
        for part in self.senior_admin_usernames_raw.replace(";", ",").replace(" ", ",").split(","):
            name = part.strip().lstrip("@")
            if name.lower().startswith(("https://t.me/", "t.me/")):
                name = name.split("t.me/", 1)[1].strip("/")
            if USERNAME_RE.fullmatch(name):
                result.setdefault(name.lower(), name)
        return result

    @property
    def senior_ids(self) -> set[int]:
        ids: set[int] = set()
        for part in self.senior_admin_ids_raw.replace(";", ",").replace(" ", ",").split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids

    def is_pinned(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._pinned

    @property
    def staff(self) -> dict[int, int]:
        """Все админы: {user_id: уровень}. Владельцы - уровень 2."""
        result = {uid: lvl for uid, lvl in self._staff.items() if lvl > 0}
        for uid in self.admin_ids:
            result[uid] = 2
        return result

    @property
    def staff_ids(self) -> set[int]:
        return set(self.staff)

    def admin_level(self, user_id: int | None) -> int:
        if user_id is None:
            return 0
        if user_id in self.admin_ids:
            return 2
        return int(self._staff.get(user_id, 0))

    @property
    def webapp_url(self) -> str | None:
        """Базовый https-адрес Mini App или None (тогда файлы уходят документами)."""
        url = self.webapp_base_url.strip().rstrip("/")
        if not url:
            return None
        if not url.startswith("https://"):
            log.warning("WEBAPP_BASE_URL должен начинаться с https://, Mini App отключены")
            return None
        return url

    # --- пути внутри data/ -------------------------------------------------
    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def solutions_dir(self) -> Path:
        return self.data_dir / "solutions"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.backups_dir, self.solutions_dir, self.uploads_dir, self.logs_dir,
                  self.exports_dir):
            p.mkdir(parents=True, exist_ok=True)
        db = sqlite_file(self.database_url)
        if db is not None:
            db.parent.mkdir(parents=True, exist_ok=True)


def sqlite_file(url: str) -> Path | None:
    """Путь к файлу SQLite из DATABASE_URL (None для других СУБД и :memory:)."""
    from sqlalchemy.engine import make_url

    u = make_url(url)
    if not u.drivername.startswith("sqlite") or not u.database or u.database == ":memory:":
        return None
    return Path(u.database)


@lru_cache
def get_config() -> Config:
    return Config()
