"""Рассылки: сегменты и отправка не быстрее 25 сообщений в секунду."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import distinct, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BonusLot, Broadcast, Order, User
from app.utils.timeutil import now_utc

log = logging.getLogger(__name__)
RATE = 25  # сообщений в секунду, не больше

SEGMENTS = {
    "all": "Все",
    "new": "Новые без заказов",
    "expiring": "Сгорают бонусы (7 дней)",
    "paid": "Оплатившие",
}


async def segment_user_ids(session: AsyncSession, segment: str, now: datetime) -> list[int]:
    base = select(User.id).where(User.is_banned.is_(False), User.bot_blocked.is_(False))
    if segment == "new":
        base = base.where(User.id.not_in(select(Order.user_id).where(Order.submitted_at.is_not(None))))
    elif segment == "expiring":
        base = base.where(User.id.in_(select(distinct(BonusLot.user_id)).where(
            BonusLot.remaining > 0, BonusLot.expired.is_(False), BonusLot.expires_at > now,
            BonusLot.expires_at <= now + timedelta(days=7))))
    elif segment == "paid":
        base = base.where(User.id.in_(select(Order.user_id).where(Order.paid_at.is_not(None))))
    elif segment != "all":
        raise ValueError(segment)
    return list((await session.execute(base.order_by(User.id))).scalars())


async def run_broadcast(bot: Bot, sessionmaker: async_sessionmaker, broadcast_id: int) -> Broadcast:
    async with sessionmaker() as session:
        b = await session.get(Broadcast, broadcast_id)
        ids = await segment_user_ids(session, b.segment, now_utc())
        b.status, b.total, b.started_at = "sending", len(ids), now_utc()
        await session.commit()
    delivered = blocked = failed = 0
    blocked_ids: list[int] = []
    for uid in ids:
        for attempt in range(3):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=b.from_chat_id, message_id=b.message_id)
                delivered += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                blocked += 1
                blocked_ids.append(uid)
                break
            except TelegramBadRequest as e:
                log.info("broadcast %s: %s → %s", broadcast_id, uid, e)
                failed += 1
                break
            except Exception:  # noqa: BLE001
                log.exception("broadcast %s: ошибка отправки %s", broadcast_id, uid)
                if attempt == 2:
                    failed += 1
                await asyncio.sleep(1)
        await asyncio.sleep(1 / RATE)
    async with sessionmaker() as session:
        if blocked_ids:
            await session.execute(update(User).where(User.id.in_(blocked_ids)).values(bot_blocked=True))
        b = await session.get(Broadcast, broadcast_id)
        b.delivered, b.blocked, b.failed = delivered, blocked, failed
        b.status, b.finished_at = "done", now_utc()
        await session.commit()
        return b
