from __future__ import annotations

import html
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.config import USERNAME_RE
from app.db.models import Referral, User

REF_RE = re.compile(r"^ref_([A-Za-z0-9_-]{4,32})$")
SRC_RE = re.compile(r"^src_([A-Za-z0-9_-]{1,40})$")


def parse_start_payload(payload: str | None) -> tuple[str, str] | None:
    if not payload:
        return None
    payload = payload.strip()
    if m := REF_RE.match(payload):
        return "ref", m.group(1)
    if m := SRC_RE.match(payload):
        return "src", m.group(1).lower()
    return None


async def _unique_ref_code(session: AsyncSession) -> str:
    while True:
        code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "z")
        if not await session.scalar(select(User.id).where(User.ref_code == code)):
            return code


@dataclass
class Registration:
    user: User
    created: bool
    referrer: User | None = None


async def register_user(session: AsyncSession, tg_user, payload: str | None, now: datetime) -> Registration:  # noqa: ANN001
    """Находит пользователя или создаёт нового. Реферал/метка источника - только при первом входе."""
    user = await session.get(User, tg_user.id)
    if user is not None:
        changed = False
        for field in ("username", "first_name", "last_name"):
            value = getattr(tg_user, field, None)
            if getattr(user, field) != value:
                setattr(user, field, value)
                changed = True
        if user.bot_blocked:
            user.bot_blocked = False
            changed = True
        if user.last_seen_at is None or now - user.last_seen_at > timedelta(minutes=5):
            user.last_seen_at = now
            changed = True
        if changed:
            await session.commit()
        return Registration(user, False)

    parsed = parse_start_payload(payload)
    source, tag, referrer = "direct", None, None
    if parsed and parsed[0] == "ref":
        candidate = await session.scalar(select(User).where(User.ref_code == parsed[1]))
        if candidate is not None and candidate.id != tg_user.id:
            referrer, source = candidate, "ref"
    elif parsed and parsed[0] == "src":
        source, tag = "src", parsed[1]

    user = User(id=tg_user.id, username=tg_user.username, first_name=tg_user.first_name,
                last_name=getattr(tg_user, "last_name", None), created_at=now, last_seen_at=now,
                source=source, source_tag=tag, referrer_id=referrer.id if referrer else None,
                ref_code=await _unique_ref_code(session))
    session.add(user)
    try:
        await session.flush()
        if referrer is not None:
            session.add(Referral(referrer_id=referrer.id, referred_id=user.id, created_at=now))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(User, tg_user.id, populate_existing=True)
        if existing is None:
            raise
        return Registration(existing, False)
    return Registration(user, True, referrer)


def display_name(user: User | None) -> str:
    """«Иван (@ivan, id 123)» - уже экранировано для parse_mode=HTML."""
    if user is None:
        return "-"
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or "без имени"
    extra = f"@{user.username}, " if user.username else ""
    return f"{html.escape(name)} ({html.escape(extra)}id <code>{user.id}</code>)"


def short_name(user: User | None) -> str:
    if user is None:
        return "-"
    name = user.first_name or (f"@{user.username}" if user.username else str(user.id))
    return html.escape(name)


async def search_users(session: AsyncSession, query: str, limit: int = 20) -> list[User]:
    q = query.strip()
    if q.lstrip("-").isdigit():
        user = await session.get(User, int(q))
        return [user] if user else []
    q = q.lstrip("@").lower()
    if not q:
        return []
    like = f"%{q}%"
    res = await session.execute(
        select(User).where(or_(func.lower(User.username).like(like), func.lower(User.first_name).like(like)))
        .order_by(User.created_at.desc()).limit(limit)
    )
    return list(res.scalars().all())


_TME_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/@?([A-Za-z0-9_]+)/?$", re.IGNORECASE)


def parse_user_query(text: str | None) -> tuple[str, int | str] | None:
    """«@Name», «name», «t.me/name», «123456789» → ("username", "name") / ("id", 123456789) или None."""
    q = (text or "").strip()
    if not q:
        return None
    if q.isdigit():
        return "id", int(q)
    if m := _TME_RE.match(q):
        q = m.group(1)
    q = q.lstrip("@")
    if USERNAME_RE.fullmatch(q):
        return "username", q.lower()
    return None


async def find_user(session: AsyncSession, text: str | None) -> tuple[User | None, str | None]:
    """Точный поиск для действий админа 2 уровня: @username (без учёта регистра) или ID.

    Возвращает (пользователь, None) или (None, текст ошибки). Пользователь должен хотя бы раз открыть бота."""
    parsed = parse_user_query(text)
    if parsed is None:
        shown = html.escape((text or "").strip()[:64]) or "-"
        return None, texts.USER_NOT_FOUND.format(query=f"«{shown}»")
    kind, value = parsed
    if kind == "id":
        user = await session.get(User, value, populate_existing=True)
        return (user, None) if user is not None else (None, texts.USER_NOT_FOUND.format(query=f"id {value}"))
    rows = list((await session.execute(select(User).where(func.lower(User.username) == value)
                                       .order_by(User.last_seen_at.desc()).limit(5)
                                       .execution_options(populate_existing=True))).scalars())
    if not rows:
        return None, texts.USER_NOT_FOUND.format(query=f"@{html.escape(str(value))}")
    if len(rows) > 1:  # ник сменил владельца, а старый владелец ещё не заходил - пусть админ уточнит ID
        ids = ", ".join(f"<code>{u.id}</code>" for u in rows)
        return None, texts.USER_AMBIGUOUS.format(query=f"@{html.escape(str(value))}", ids=ids)
    return rows[0], None


async def paid_friends(session: AsyncSession, referrer_id: int) -> int:
    return int(await session.scalar(
        select(func.count(Referral.id)).where(Referral.referrer_id == referrer_id, Referral.rewarded.is_(True))
    ) or 0)


async def referrals_of(session: AsyncSession, referrer_id: int) -> list[tuple[Referral, User]]:
    res = await session.execute(
        select(Referral, User).join(User, User.id == Referral.referred_id)
        .where(Referral.referrer_id == referrer_id).order_by(Referral.created_at.desc())
    )
    return [(r, u) for r, u in res.all()]


def free_month_active(user: User, now: datetime) -> bool:
    return bool(user.free_month_until and user.free_month_until > now and user.free_month_tasks_left > 0)
