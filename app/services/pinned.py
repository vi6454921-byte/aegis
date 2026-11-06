"""Закреплённые админы 2 уровня (v2.2): SENIOR_ADMIN_USERNAMES и SENIOR_ADMIN_IDS в .env.

• Ник из .env привязывается к Telegram id при первом сообщении боту (запись в pinned_admins + admins.pinned).
  Дальше права - только по id: смена ника их не сохраняет «за ником», новый владелец ника не получает ничего.
• Каждый ник привязывается один раз (уникальный индекс). Попытка написать с этим ником с другого id -   предупреждение в лог, запись в admin_log и одно уведомление владельцам.
• Снять или понизить закреплённого может только владелец (тогда закрепление снимается и по нику больше не
  выдаётся, пока владелец не вернёт его кнопкой) или удаление ника/id из .env (при следующем запуске).
• Ник вернули в .env после удаления - закрепление восстанавливается при следующем сообщении этого же id.

Отправка сообщений - в app/bot/pinned.py; здесь только база и кэш в config.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.config import Config
from app.db.models import Admin, PinnedAdmin
from app.services import roles
from app.services.admin_log import log_admin

log = logging.getLogger(__name__)
SYSTEM = 0  # admin_id в журнале для действий самого бота (синхронизация с .env)


@dataclass
class BindResult:
    kind: str  # bound - закреплён; spoof - ник уже привязан к другому id
    user_id: int
    username: str
    bound_id: int
    first_alert: bool = True
    owner: bool = False


@dataclass
class SyncResult:
    bound: list[int] = field(default_factory=list)  # закреплены по SENIOR_ADMIN_IDS (новые или вернули)
    removed: list[tuple[str, int, str]] = field(default_factory=list)  # (кто, id, переменная .env) - сняты


def wants(config: Config, tg_user) -> bool:  # noqa: ANN001
    """Быстрая проверка в middleware: нужно ли что-то делать с этим пользователем (без запросов к БД)."""
    name = (getattr(tg_user, "username", None) or "").lower()
    if not name or name not in config.senior_usernames:
        return False
    bind = config._pinned_bind.get(name)  # noqa: SLF001
    if bind is None:
        return True
    bound_id, state = bind
    if bound_id != tg_user.id:
        return (name, tg_user.id) not in config._spoof_alerted  # noqa: SLF001
    return state == "env"


def _insert(session: AsyncSession, model):  # noqa: ANN001, ANN202
    """INSERT ... ON CONFLICT для текущей базы (SQLite или PostgreSQL)."""
    dialect = session.get_bind().dialect.name
    return (postgresql if dialect == "postgresql" else sqlite).insert(model)


async def _upsert_admin(session: AsyncSession, config: Config, user_id: int, now: datetime) -> None:
    """v2.4: идемпотентно. Раньше тут был session.get() + session.add(): при нескольких одновременных /start
    autoflush внутри get() вставлял строку второй раз и падал с IntegrityError мимо try/except у commit.
    Теперь одна команда INSERT ... ON CONFLICT DO UPDATE: сколько раз ни вызови, строка одна, уровень 2."""
    if user_id in config.admin_ids:  # владелец и так 2 уровня, строка в admins не нужна
        return
    stmt = _insert(session, Admin).values(user_id=user_id, level=2, added_by=None, added_at=now, pinned=True)
    await session.execute(stmt.on_conflict_do_update(index_elements=[Admin.user_id],
                                                     set_={"level": 2, "pinned": True}))
    loaded = session.identity_map.get(identity_key(Admin, user_id))
    if loaded is not None:  # объект уже в сессии: синхронизируем без лишнего UPDATE
        set_committed_value(loaded, "level", 2)
        set_committed_value(loaded, "pinned", True)


async def _insert_pinned(session: AsyncSession, name: str, uid: int, now: datetime) -> bool:
    """Привязка ника: INSERT ... ON CONFLICT (username) DO NOTHING. False: ник уже привязал параллельный апдейт."""
    stmt = _insert(session, PinnedAdmin).values(username=name, user_id=uid, source="username", bound_at=now)
    res = await session.execute(stmt.on_conflict_do_nothing(index_elements=[PinnedAdmin.username]))
    return bool(res.rowcount)


async def on_user(session: AsyncSession, config: Config, tg_user, now: datetime) -> BindResult | None:  # noqa: ANN001
    """Пользователь написал боту: привязать ник из .env к его id (или зафиксировать попытку подмены)."""
    if not wants(config, tg_user):
        return None
    name = tg_user.username.lower()
    uid = int(tg_user.id)
    bind = config._pinned_bind.get(name)  # noqa: SLF001
    if bind is not None and bind[0] != uid:
        config._spoof_alerted.add((name, uid))  # noqa: SLF001
        log.warning("⚠️ попытка подмены закреплённого админа: @%s привязан к id %s, пишет id %s",
                    tg_user.username, bind[0], uid)
        log_admin(session, uid, "pinned_spoof", "user", bind[0], username=name)
        await session.commit()
        return BindResult("spoof", uid, tg_user.username, bind[0])
    row = await session.scalar(select(PinnedAdmin).where(PinnedAdmin.username == name))
    if row is not None and (row.user_id != uid or row.revoked_by):  # кэш устарел - перечитаем и решим в след. раз
        await roles.load(session, config)
        return None
    if row is not None and row.revoked_at is None:  # v2.4: уже закрепил параллельный апдейт, кэш отстал
        with session.no_autoflush:
            await _upsert_admin(session, config, uid, now)  # идемпотентно: строка одна, уровень 2
        await session.commit()
        await roles.load(session, config)
        return None
    try:
        with session.no_autoflush:
            if row is None:
                if not await _insert_pinned(session, name, uid, now):  # уже привязал параллельный /start
                    await session.rollback()
                    await roles.load(session, config)
                    return None
            else:  # ник вернули в .env: восстанавливаем закрепление того же id
                row.revoked_at, row.revoked_by = None, None
            await _upsert_admin(session, config, uid, now)
        log_admin(session, uid, "pinned_bind", "user", uid, username=name, restored=row is not None)
        await session.commit()
    except IntegrityError:  # на всякий случай: тот же ник одновременно привязали в другом процессе
        await session.rollback()
        await roles.load(session, config)
        return None
    await roles.load(session, config)
    log.warning("📌 закреплён админ 2 уровня: @%s (id %s)", tg_user.username, uid)
    return BindResult("bound", uid, tg_user.username, uid, owner=uid in config.admin_ids)


async def sync(session: AsyncSession, config: Config, now: datetime) -> SyncResult:
    """При запуске: сверить закрепления с .env. Убранных из .env - снять, новые id из SENIOR_ADMIN_IDS - закрепить."""
    names, ids = config.senior_usernames, config.senior_ids
    rows = list((await session.execute(select(PinnedAdmin))).scalars())
    res = SyncResult()
    touched: dict[int, tuple[str, str]] = {}
    for row in rows:
        if row.revoked_at is not None:
            continue
        if row.source == "username" and row.username not in names:
            row.revoked_at, row.revoked_by = now, None
            touched[row.user_id] = (f"@{row.username}", "SENIOR_ADMIN_USERNAMES")
        elif row.source == "id" and row.user_id not in ids:
            row.revoked_at, row.revoked_by = now, None
            touched[row.user_id] = (f"id {row.user_id}", "SENIOR_ADMIN_IDS")
    by_id = {row.user_id: row for row in rows if row.source == "id"}
    for uid in sorted(ids):
        row = by_id.get(uid)
        if row is None:
            session.add(PinnedAdmin(username=None, user_id=uid, source="id", bound_at=now))
            res.bound.append(uid)
        elif row.revoked_at is not None and not row.revoked_by:
            row.revoked_at = None
            res.bound.append(uid)
        elif row.revoked_at is not None:  # снят владельцем - не возвращаем сами
            continue
        await _upsert_admin(session, config, uid, now)
    await session.flush()
    active = set((await session.execute(select(PinnedAdmin.user_id)
                                        .where(PinnedAdmin.revoked_at.is_(None)))).scalars())
    for uid, (who, source) in touched.items():
        if uid in active:  # закреплён ещё и по другому нику или id
            continue
        admin = await session.get(Admin, uid)
        if admin is not None and admin.pinned:
            await session.delete(admin)
        res.removed.append((who, uid, source))
    if res.bound or res.removed:
        log_admin(session, SYSTEM, "pinned_env_sync", None, None, bound=res.bound,
                  removed=[uid for _, uid, _ in res.removed])
    await session.commit()
    await roles.load(session, config)
    return res


async def restore(session: AsyncSession, config: Config, pinned_id: int, actor_id: int, now: datetime) -> int:
    """Владелец возвращает закрепление, которое сам снял (ник или id всё ещё в .env). Возвращает id админа."""
    if not roles.is_owner(config, actor_id):
        raise roles.RoleError("Вернуть закрепление может только владелец.")
    row = await session.get(PinnedAdmin, pinned_id)
    if row is None or row.revoked_at is None:
        raise roles.RoleError("Закрепление уже действует.")
    in_env = row.username in config.senior_usernames if row.source == "username" else row.user_id in config.senior_ids
    if not in_env:
        raise roles.RoleError("Этого админа уже нет в .env: добавьте его туда и перезапустите бота.")
    row.revoked_at, row.revoked_by = None, None
    await _upsert_admin(session, config, row.user_id, now)
    log_admin(session, actor_id, "pinned_restore", "user", row.user_id, username=row.username)
    await session.commit()
    await roles.load(session, config)
    return row.user_id


async def entries(session: AsyncSession) -> list[PinnedAdmin]:
    return list((await session.execute(select(PinnedAdmin).order_by(PinnedAdmin.id))).scalars())
