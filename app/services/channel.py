"""Канал: бонус за подписку и бонус за друга, который пришёл по ссылке и подписался.

Правила и защита от накруток:
• Бонус за подписку — один раз на аккаунт. Отписался в течение срока удержания (channel_hold_hours) —
  бонус списывается (из остатка, не ниже нуля) и больше не начисляется никогда.
• Друг засчитывается, только если впервые зашёл в бота по ссылке (это решается при регистрации) и сам
  подписался на канал. Пригласивший получает бонус после срока удержания, если друг всё ещё подписан и
  пригласивший тоже подписан. Отписка друга до конца удержания отменяет бонус за него навсегда.
• За сутки пригласившему засчитывается не больше channel_ref_daily_limit друзей — остальные ждут следующего дня.
• Забаненным — ничего. Проверка подписки — через getChatMember: бот должен быть админом канала.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BonusLot, Referral, User
from app.services.ledger import credit_bonus, debit_bonus, lock_user
from app.utils.timeutil import end_of_local_day, local_today

log = logging.getLogger(__name__)

MEMBER = "member"
LEFT = "left"
PENDING_TTL = timedelta(days=30)  # друг подписался, а пригласивший так и не подписался — через 30 дней отменяем
RECHECK_LEFT = timedelta(hours=6)  # как часто перепроверять неподписанного пригласившего


@dataclass
class Health:
    """Последняя ошибка проверки подписки — показываем в админке."""
    error: str | None = None
    error_at: datetime | None = None
    ok_at: datetime | None = None
    alerted_at: datetime | None = None


health = Health()

Checker = Callable[[int], Awaitable[str | None]]


def member_status(m) -> str:  # noqa: ANN001
    status = str(getattr(m, "status", "") or "")
    status = status.split(".")[-1].lower()  # ChatMemberStatus.MEMBER → member
    if status in ("member", "administrator", "creator"):
        return MEMBER
    if status == "restricted":
        return MEMBER if getattr(m, "is_member", False) else LEFT
    return LEFT


async def fetch_status(bot: Bot, channel: str, user_id: int, now: datetime | None = None) -> str | None:
    """«member» / «left» / None — не получилось проверить (бот не админ канала, канал не найден, сеть)."""
    if not channel:
        return None
    try:
        m = await bot.get_chat_member(channel, user_id)
    except TelegramRetryAfter as e:
        log.warning("getChatMember: flood wait %s", e.retry_after)
        return None
    except TelegramAPIError as e:
        health.error, health.error_at = str(e), now
        log.warning("getChatMember(%s, %s) не удался: %s", channel, user_id, e)
        return None
    except Exception as e:  # noqa: BLE001 — сеть и т.п.
        health.error, health.error_at = str(e), now
        log.warning("getChatMember(%s, %s): %s", channel, user_id, e)
        return None
    health.error, health.ok_at = None, now
    return member_status(m)


def failing() -> bool:
    return health.error is not None


async def bot_rights_problem(bot: Bot, channel: str, now: datetime | None = None) -> str | None:
    """Проверка при запуске: может ли бот видеть подписчиков канала. None — всё в порядке, иначе — что не так."""
    if not channel:
        return None
    try:
        m = await bot.get_chat_member(channel, bot.id)
    except TelegramRetryAfter:
        return None  # не знаем — проверим позже на живых запросах
    except Exception as e:  # noqa: BLE001 — канал не найден, бота нет в канале, сеть
        problem = f"канал {channel} недоступен боту ({e})"
    else:
        status = str(getattr(m, "status", "") or "").split(".")[-1].lower()
        if status in ("administrator", "creator"):
            health.error, health.ok_at = None, now
            return None
        problem = f"бот не администратор канала {channel}"
    health.error, health.error_at = problem, now
    return problem


def should_alert(now: datetime) -> bool:
    """Предупреждать старших админов о сломанной проверке не чаще раза в 6 часов."""
    if health.error is None:
        return False
    if health.alerted_at is not None and now - health.alerted_at < timedelta(hours=6):
        return False
    health.alerted_at = now
    return True


# ---------------------------------------------------------------- подписался / отписался
@dataclass
class JoinResult:
    granted: int = 0  # начислено самому пользователю
    expires_at: datetime | None = None
    already_at: datetime | None = None  # бонус уже был начислен раньше
    revoked_before: bool = False  # бонус был, но списан за отписку — повторно нельзя
    referrer_id: int | None = None  # друг подписался — у пригласившего ожидается бонус
    ref_amount: int = 0
    ref_due_at: datetime | None = None
    banned: bool = False


async def on_member(session: AsyncSession, user_id: int, s, now: datetime) -> JoinResult:  # noqa: ANN001
    """Пользователь подписан (событие канала или кнопка «проверить»). Вызывать под app_lock, коммитит."""
    user = await lock_user(session, user_id)
    res = JoinResult()
    user.channel_status, user.channel_checked_at = MEMBER, now
    if user.is_banned:
        res.banned = True
        await session.commit()
        return res
    if user.channel_bonus_revoked_at is not None:
        res.revoked_before = True
    elif user.channel_bonus_at is not None:
        res.already_at = user.channel_bonus_at
    elif s.channel_bonus_on:
        amount = int(s.channel_bonus)
        lot = await credit_bonus(session, user, amount, source="channel", kind="channel_bonus",
                                 ttl_days=int(s.bonus_ttl_days), now=now, comment=s.channel)
        user.channel_bonus_at, user.channel_bonus_amount = now, amount
        res.granted, res.expires_at = amount, lot.expires_at
    if s.channel_ref_on:
        ref = await session.scalar(select(Referral).where(Referral.referred_id == user.id))
        if ref is not None and ref.sub_status == "none":
            referrer = await session.get(User, ref.referrer_id)
            if referrer is not None and not referrer.is_banned:
                ref.sub_status = "pending"
                ref.sub_due_at = now + timedelta(hours=int(s.channel_hold_hours))
                res.referrer_id, res.ref_amount, res.ref_due_at = referrer.id, int(s.channel_ref_bonus), ref.sub_due_at
    await session.commit()
    return res


@dataclass
class LeaveResult:
    revoked: int = 0
    was_granted: bool = False
    cancelled_referral: bool = False


async def on_left(session: AsyncSession, user_id: int, s, now: datetime) -> LeaveResult:  # noqa: ANN001
    """Пользователь отписался. Вызывать под app_lock, коммитит."""
    user = await session.get(User, user_id)
    res = LeaveResult()
    if user is None:
        return res
    user = await lock_user(session, user_id)
    user.channel_status, user.channel_checked_at = LEFT, now
    hold = timedelta(hours=int(s.channel_hold_hours))
    if user.channel_bonus_at is not None and user.channel_bonus_revoked_at is None \
            and now - user.channel_bonus_at < hold:
        lot = await session.scalar(select(BonusLot).where(BonusLot.user_id == user.id, BonusLot.source == "channel")
                                   .order_by(BonusLot.id.desc()).limit(1))
        res.was_granted = True
        res.revoked = await debit_bonus(session, user, int(user.channel_bonus_amount), kind="channel_bonus_revoke",
                                        now=now, allow_partial=True, prefer_lot_id=lot.id if lot else None,
                                        comment="отписка в течение срока удержания")
        user.channel_bonus_revoked_at = now
    ref = await session.scalar(select(Referral).where(Referral.referred_id == user.id))
    if ref is not None and ref.sub_status == "pending":
        ref.sub_status = "cancelled"
        res.cancelled_referral = True
    await session.commit()
    return res


# ---------------------------------------------------------------- бонус пригласившему
@dataclass
class RefEvent:
    kind: str  # reward | need_sub
    referrer_id: int
    friend_id: int
    amount: int
    expires_at: datetime | None = None


@dataclass
class DueReport:
    events: list[RefEvent] = field(default_factory=list)
    rewarded: int = 0
    cancelled: int = 0
    postponed: int = 0


async def rewarded_today(session: AsyncSession, referrer_id: int, now: datetime) -> int:
    start = end_of_local_day(local_today(now)) - timedelta(days=1)
    return int(await session.scalar(select(func.count(Referral.id)).where(
        Referral.referrer_id == referrer_id, Referral.sub_status == "rewarded", Referral.sub_rewarded_at > start)) or 0)


async def _status(session: AsyncSession, user: User, checker: Checker, now: datetime, *, cached_left_ok: bool) -> str | None:
    """Статус подписки с кэшем: неподписанного пригласившего перепроверяем не чаще RECHECK_LEFT."""
    if cached_left_ok and user.channel_status == LEFT and user.channel_checked_at is not None \
            and now - user.channel_checked_at < RECHECK_LEFT:
        return LEFT
    status = await checker(user.id)
    if status is not None:
        user.channel_status, user.channel_checked_at = status, now
    return status


async def process_due(session: AsyncSession, s, now: datetime, checker: Checker,  # noqa: ANN001, C901
                      referrer_id: int | None = None, fresh: bool = False) -> DueReport:
    """Начисляет бонусы пригласившим, у кого прошёл срок удержания. Вызывать под app_lock, коммитит.
    referrer_id — только для одного пригласившего (он нажал «проверить» или подписался); fresh — не верить кэшу."""
    report = DueReport()
    if not s.channel:
        return report
    q = select(Referral).where(Referral.sub_status == "pending", Referral.sub_due_at <= now)
    if referrer_id is not None:
        q = q.where(Referral.referrer_id == referrer_id)
    refs = list((await session.execute(q.order_by(Referral.sub_due_at, Referral.id).limit(200))).scalars())
    limit = max(int(s.channel_ref_daily_limit), 1)
    for ref in refs:
        friend = await session.get(User, ref.referred_id)
        referrer = await session.get(User, ref.referrer_id)
        if friend is None or referrer is None or friend.is_banned or referrer.is_banned or not s.channel_ref_on:
            ref.sub_status = "cancelled"
            report.cancelled += 1
            continue
        f_status = await _status(session, friend, checker, now, cached_left_ok=False)
        if f_status is None:
            continue  # проверить не удалось — попробуем в следующий раз
        if f_status == LEFT:
            ref.sub_status = "cancelled"
            report.cancelled += 1
            continue
        r_status = await _status(session, referrer, checker, now, cached_left_ok=not fresh)
        if r_status is None:
            continue
        if r_status == LEFT:
            if ref.sub_due_at is not None and now - ref.sub_due_at > PENDING_TTL:
                ref.sub_status = "cancelled"
                report.cancelled += 1
            elif not ref.sub_notified:
                ref.sub_notified = True
                report.events.append(RefEvent("need_sub", referrer.id, friend.id, int(s.channel_ref_bonus)))
            continue
        if await rewarded_today(session, referrer.id, now) >= limit:
            ref.sub_due_at = end_of_local_day(local_today(now)) + timedelta(minutes=1)
            report.postponed += 1
            continue
        locked = await lock_user(session, referrer.id)
        amount = int(s.channel_ref_bonus)
        lot = await credit_bonus(session, locked, amount, source="ref_channel", kind="referral_channel",
                                 ttl_days=int(s.bonus_ttl_days), now=now, referral_id=ref.id,
                                 comment=f"друг {friend.id} подписался на {s.channel}")
        ref.sub_status, ref.sub_rewarded_at, ref.sub_reward_amount = "rewarded", now, amount
        report.rewarded += 1
        report.events.append(RefEvent("reward", referrer.id, friend.id, amount, lot.expires_at))
        await session.flush()
    await session.commit()
    return report


async def sub_counts(session: AsyncSession, referrer_id: int) -> dict[str, int]:
    rows = (await session.execute(select(Referral.sub_status, func.count(Referral.id))
                                  .where(Referral.referrer_id == referrer_id).group_by(Referral.sub_status))).all()
    return {str(k): int(v) for k, v in rows}
