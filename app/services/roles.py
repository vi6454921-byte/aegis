"""Уровни админов.

• Владельцы — ADMIN_IDS в .env (или назначенные кодом «/admin КОД»). Всегда 2-й уровень, из бота не снимаются.
• Закреплённые админы 2 уровня (📌, v2.2) — SENIOR_ADMIN_USERNAMES / SENIOR_ADMIN_IDS в .env. Другие админы
  2 уровня и приглашения их не снимают и не понижают — только владелец или удаление из .env (app/services/pinned.py).
• 2-й уровень (старший): всё — настройки, рассылки, балансы, баны, возвраты, выгрузки, управление админами 1-го уровня.
• 1-й уровень (оператор): заказы (проверка, счёт, работа, решение), подтверждение оплаты переводом,
  обращения в поддержку, сообщения клиентам, проверка заданий.

Назначенные в боте админы хранятся в таблице admins; в памяти процесса — кэш в config (config.staff).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.db.models import Admin, AdminInvite, PinnedAdmin
from app.services.admin_log import log_admin
from app.utils.timeutil import now_utc

LEVELS = {1: "1 уровень · оператор", 2: "2 уровень · старший"}
LEVEL_SHORT = {1: "ур. 1", 2: "ур. 2"}
INVITE_TTL = timedelta(hours=24)


class RoleError(Exception):
    """Текст для человека."""


def bind_state(row: PinnedAdmin) -> str:
    """active — закрепление действует; owner — снято владельцем в боте; env — ника/id больше нет в .env."""
    if row.revoked_at is None:
        return "active"
    return "owner" if row.revoked_by else "env"


async def load(session: AsyncSession, config: Config) -> dict[int, int]:
    rows = (await session.execute(select(Admin.user_id, Admin.level, Admin.pinned))).all()
    config._staff = {int(uid): int(lvl) for uid, lvl, _ in rows if lvl in LEVELS}  # noqa: SLF001
    config._pinned = {int(uid) for uid, lvl, pinned in rows if pinned and lvl in LEVELS}  # noqa: SLF001
    binds = (await session.execute(select(PinnedAdmin).where(PinnedAdmin.username.is_not(None)))).scalars()
    config._pinned_bind = {b.username: (int(b.user_id), bind_state(b)) for b in binds}  # noqa: SLF001
    config._staff_loaded = True  # noqa: SLF001
    return dict(config._staff)  # noqa: SLF001


async def ensure_loaded(session: AsyncSession, config: Config) -> None:
    if not config._staff_loaded:  # noqa: SLF001
        await load(session, config)


def is_owner(config: Config, user_id: int) -> bool:
    return user_id in config.admin_ids


def can_manage(config: Config, actor_id: int, target_id: int | None, target_level: int) -> bool:
    """Владелец управляет всеми, кроме владельцев; старший (2) — только операторами (1), но не закреплёнными."""
    if target_id is not None and is_owner(config, target_id):
        return False
    if target_id is not None and target_id == actor_id:
        return False
    if is_owner(config, actor_id):
        return target_level in LEVELS
    if target_id is not None and config.is_pinned(target_id):
        return False
    return config.admin_level(actor_id) >= 2 and target_level == 1


async def _unpin(session: AsyncSession, user_id: int, actor_id: int, now: datetime) -> bool:
    """Владелец снимает закрепление: по нику из .env права этому человеку больше не выдаются. Без commit."""
    rows = (await session.execute(select(PinnedAdmin).where(PinnedAdmin.user_id == user_id,
                                                            PinnedAdmin.revoked_at.is_(None)))).scalars().all()
    for row in rows:
        row.revoked_at, row.revoked_by = now, actor_id
    return bool(rows)


async def set_level(session: AsyncSession, config: Config, user_id: int, level: int, actor_id: int,
                    now: datetime) -> None:
    current = config.admin_level(user_id)
    if not can_manage(config, actor_id, user_id, max(level, current)):
        raise RoleError("Недостаточно прав для этого действия.")
    pinned = config.is_pinned(user_id)
    row = await session.get(Admin, user_id)
    if row is None:
        session.add(Admin(user_id=user_id, level=level, added_by=actor_id, added_at=now))
    else:
        row.level = level
        if pinned and level < 2:  # владелец понизил закреплённого — закрепление снимается
            row.pinned = False
            await _unpin(session, user_id, actor_id, now)
    log_admin(session, actor_id, "admin_set_level", "user", user_id, old=current, new=level,
              unpinned=pinned and level < 2)
    await session.commit()
    if pinned and level < 2:
        await load(session, config)
    config._staff[user_id] = level  # noqa: SLF001


async def remove(session: AsyncSession, config: Config, user_id: int, actor_id: int) -> None:
    current = config.admin_level(user_id)
    if current == 0:
        raise RoleError("Этот человек уже не админ.")
    if not can_manage(config, actor_id, user_id, current):
        raise RoleError("Недостаточно прав, чтобы снять этого админа.")
    pinned = config.is_pinned(user_id)
    if pinned:
        await _unpin(session, user_id, actor_id, now_utc())
    await session.execute(delete(Admin).where(Admin.user_id == user_id))
    log_admin(session, actor_id, "admin_remove", "user", user_id, old=current, unpinned=pinned)
    await session.commit()
    if pinned:
        await load(session, config)
    config._staff.pop(user_id, None)  # noqa: SLF001


async def create_invite(session: AsyncSession, config: Config, level: int, actor_id: int, now: datetime) -> str:
    if not can_manage(config, actor_id, None, level):
        raise RoleError("Недостаточно прав, чтобы приглашать админов этого уровня.")
    token = secrets.token_urlsafe(12).replace("-", "x").replace("_", "z")
    session.add(AdminInvite(token=token, level=level, created_by=actor_id, created_at=now,
                            expires_at=now + INVITE_TTL))
    log_admin(session, actor_id, "admin_invite", None, None, level=level)
    await session.commit()
    return token


@dataclass
class InviteResult:
    level: int
    inviter_id: int


async def accept_invite(session: AsyncSession, config: Config, token: str, user_id: int,
                        now: datetime) -> InviteResult:
    inv = await session.get(AdminInvite, token)
    if inv is None or inv.used_by is not None:
        raise RoleError("Ссылка-приглашение уже использована или не существует. Попроси новую.")
    if inv.expires_at <= now:
        raise RoleError("Срок действия приглашения истёк. Попроси новую ссылку.")
    if config.admin_level(inv.created_by) < 2:
        raise RoleError("Пригласивший больше не администратор — приглашение не действует.")
    # владельца и закреплённого приглашение не меняет (закреплённого нельзя понизить ссылкой 1 уровня)
    if is_owner(config, user_id) or config.is_pinned(user_id) or config.admin_level(user_id) >= inv.level:
        inv.used_by, inv.used_at = user_id, now
        await session.commit()
        return InviteResult(config.admin_level(user_id), inv.created_by)
    row = await session.get(Admin, user_id)
    if row is None:
        session.add(Admin(user_id=user_id, level=inv.level, added_by=inv.created_by, added_at=now))
    else:
        row.level = inv.level
    inv.used_by, inv.used_at = user_id, now
    log_admin(session, inv.created_by, "admin_invite_used", "user", user_id, level=inv.level)
    await session.commit()
    config._staff[user_id] = inv.level  # noqa: SLF001
    return InviteResult(inv.level, inv.created_by)


async def staff_rows(session: AsyncSession) -> dict[int, Admin]:
    return {a.user_id: a for a in (await session.execute(select(Admin))).scalars()}
