"""Фоновые задачи (APScheduler, время — Europe/Samara): бэкап, бонусы, напоминания, дедлайны, сводка.

v2.3: «Это задание?» без ответа → в поддержку (раз в минуту), черновики без движения (предупреждение за день,
затем удаление), безлимит по промокоду (напоминание за 3 дня до конца и «Безлимит закончился»)."""
from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.bot.callbacks import AdminCb, PayCb, WizCb
from app.bot.drafts import last_activity
from app.bot.handlers.admin.receipts import receipt_reminder_text
from app.bot.handlers.channel import send_ref_events
from app.bot.handlers.stray import flush_stray
from app.bot.keyboards import cb, ik, main_menu, order_alert_kb
from app.bot.notify import notify_admins, safe_send
from app.config import Config
from app.db.models import BonusLot, Order, OrderFile, User
from app.services import backup, promo, promocodes, slots, tickets
from app.services import channel as ch
from app.services import orders as svc
from app.services.ledger import expire_due_lots
from app.services.locks import app_lock
from app.services.payments import manual_queue
from app.services.settings import load_settings
from app.utils.money import fmt_rub
from app.utils.timeutil import TZ, end_of_local_day, fmt_date, fmt_dt, fmt_left, full_date, local_today, now_utc

log = logging.getLogger(__name__)
SEND_PAUSE = 0.05  # ~20 сообщений в секунду


async def burn_bonuses(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession],
                       now: datetime | None = None) -> dict[int, int]:
    """Сжигает просроченные партии бонусов и сообщает пользователям, сколько сгорело."""
    async with sessionmaker() as session:
        async with app_lock():
            burned = await expire_due_lots(session, now or now_utc())
            await session.commit()
    for user_id, amount in burned.items():
        await safe_send(bot, user_id, texts.BONUS_EXPIRED.format(amount=fmt_rub(amount)))
        await asyncio.sleep(SEND_PAUSE)
    if burned:
        log.info("сгорели бонусы у %s пользователей", len(burned))
    return burned


async def remind_bonuses(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession],
                         now: datetime | None = None) -> dict[int, int]:
    """За 3 дня до сгорания — одно напоминание на партию (сумма и ближайшая дата по пользователю)."""
    now = now or now_utc()
    horizon = end_of_local_day(local_today(now) + timedelta(days=3))
    per_user: dict[int, tuple[int, datetime]] = {}
    async with sessionmaker() as session:
        async with app_lock():
            lots = (await session.execute(
                select(BonusLot).where(BonusLot.expired.is_(False), BonusLot.reminded.is_(False),
                                       BonusLot.remaining > 0, BonusLot.expires_at > now,
                                       BonusLot.expires_at <= horizon)
                .order_by(BonusLot.user_id, BonusLot.expires_at))).scalars().all()
            for lot in lots:
                amount, first = per_user.get(lot.user_id, (0, lot.expires_at))
                per_user[lot.user_id] = (amount + lot.remaining, min(first, lot.expires_at))
                lot.reminded = True
            await session.commit()
    for user_id, (amount, first) in per_user.items():
        await safe_send(bot, user_id, texts.BONUS_REMINDER.format(amount=fmt_rub(amount), date=fmt_date(first)))
        await asyncio.sleep(SEND_PAUSE)
    return {uid: amount for uid, (amount, _) in per_user.items()}


async def remind_open(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession],
                      now: datetime | None = None) -> int:
    """Утром: тем, кто нажал «Напомнить завтра», — если приём открыт."""
    now = now or now_utc()
    async with sessionmaker() as session:
        s = await load_settings(session)
        if not (await slots.intake_state(session, s, now)).open:
            return 0
        ids = list((await session.execute(select(User.id).where(User.remind_tomorrow.is_(True),
                                                                User.is_banned.is_(False)))).scalars())
        if not ids:
            return 0
        await session.execute(update(User).where(User.id.in_(ids)).values(remind_tomorrow=False))
        await session.commit()
    sent = 0
    for user_id in ids:
        if await safe_send(bot, user_id, texts.REMIND_OPEN, reply_markup=main_menu()):
            sent += 1
        await asyncio.sleep(SEND_PAUSE)
    return sent


DEADLINE_SOON_USER = timedelta(hours=12)  # клиенту: «оплати, до дедлайна мало времени»
DEADLINE_ALERT_ADMIN = timedelta(hours=3)  # админам: «до дедлайна заказа в работе ≤ 3 ч»
RECEIPT_WAIT = timedelta(hours=1)  # чек ждёт проверки дольше — напомнить админам
WORK_STATUSES = ("review", "paid", "in_work")


def _left(deadline: datetime, now: datetime) -> str:
    return fmt_left(deadline, now).removeprefix("через ")


async def remind_unpaid(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], now: datetime | None = None) -> int:
    """Один раз на заказ: счёт висит дольше pay_reminder_hours или до дедлайна < 12 ч."""
    now = now or now_utc()
    async with sessionmaker() as session:
        s = await load_settings(session)
        hours = int(s.pay_reminder_hours)
        if hours <= 0:
            return 0
        orders = list((await session.execute(select(Order).where(
            Order.status == "awaiting_payment", Order.pay_reminded_at.is_(None),
            Order.invoiced_at.is_not(None)).order_by(Order.id).limit(200))).scalars())
        due = []
        for o in orders:
            soon = o.deadline_at is not None and o.deadline_at - now <= DEADLINE_SOON_USER
            if soon or now - o.invoiced_at >= timedelta(hours=hours):
                o.pay_reminded_at = now
                due.append((o.id, o.user_id, o.price_final or 0, o.deadline_at if soon else None))
        await session.commit()
    sent = 0
    for oid, uid, price, deadline in due:
        if deadline is not None and deadline > now:
            text = texts.DEADLINE_SOON_USER.format(id=oid, left=_left(deadline, now))
        else:
            text = texts.PAY_REMINDER.format(id=oid, price=fmt_rub(price))
        kb = ik([cb(f"{texts.PAY_BTN} · {fmt_rub(price)}", PayCb(action="order", id=oid))])
        if await safe_send(bot, uid, text, reply_markup=kb):
            sent += 1
        await asyncio.sleep(SEND_PAUSE)
    return sent


async def alert_deadlines(bot: Bot, config: Config, sessionmaker: async_sessionmaker[AsyncSession],
                          now: datetime | None = None) -> int:
    """Админам — один раз на заказ, когда до дедлайна ≤ 3 ч, а работа не сдана."""
    now = now or now_utc()
    async with sessionmaker() as session:
        orders = list((await session.execute(select(Order).where(
            Order.status.in_(WORK_STATUSES), Order.deadline_alerted_at.is_(None), Order.deadline_at.is_not(None),
            Order.deadline_at <= now + DEADLINE_ALERT_ADMIN).order_by(Order.deadline_at).limit(50))).scalars())
        for o in orders:
            o.deadline_alerted_at = now
        await session.commit()
        items = [(o.id, o.status, o.deadline_at, o.assignee_id) for o in orders]
    for oid, status, deadline, assignee in items:
        left = f"осталось {_left(deadline, now)}" if deadline > now else "срок уже прошёл"
        text = (f"⏰ <b>Дедлайн близко</b>: заказ #{oid} — {left} (до {fmt_dt(deadline)}).\n"
                f"Статус: {texts.STATUS.get(status, status)}" + ("" if assignee else " · исполнитель не назначен"))
        await notify_admins(bot, config, text, markup_for=lambda _lvl, oid=oid: order_alert_kb(oid))
    return len(items)


async def remind_receipts(bot: Bot, config: Config, sessionmaker: async_sessionmaker[AsyncSession],
                          now: datetime | None = None) -> int:
    """Чеки «я оплатил», которые ждут проверки дольше часа, — одно напоминание на чек."""
    now = now or now_utc()
    async with sessionmaker() as session:
        waiting = [p for p in await manual_queue(session, 50)
                   if p.admin_reminded_at is None and p.claimed_at is not None and now - p.claimed_at >= RECEIPT_WAIT]
        if not waiting:
            return 0
        text = await receipt_reminder_text(session, waiting)
        for p in waiting:
            p.admin_reminded_at = now
        await session.commit()
    await notify_admins(bot, config, text, reply_markup=ik([cb("🧾 Открыть чеки", AdminCb(s="pay", a="list"))]))
    return len(waiting)


async def channel_watch(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], now: datetime | None = None) -> int:
    """Бонусы пригласившим, у кого истёк срок удержания (друг всё ещё подписан)."""
    now = now or now_utc()
    async with sessionmaker() as session:
        s = await load_settings(session)
        if not s.channel:
            return 0

        async def checker(uid: int) -> str | None:
            return await ch.fetch_status(bot, s.channel, uid, now)

        async with app_lock():
            report = await ch.process_due(session, s, now, checker)
        await send_ref_events(bot, session, s, report.events)
    return report.rewarded


async def profile_recheck(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession],
                          now: datetime | None = None) -> int:
    """«📌 Ссылка в профиле»: через N дней после одобрения смотрим «О себе» (getChat). Ссылки нет — награда
    списывается (не ниже нуля) и приходит уведомление. Telegram не ответил — попробуем завтра."""
    now = now or now_utc()
    async with sessionmaker() as session:
        s = await load_settings(session)
        due = await promo.recheck_due(session, s, now)
        if not due:
            return 0
        me = await bot.me()
        revoked = 0
        for claim in due[:50]:
            user = await session.get(User, claim.user_id)
            if user is None:
                continue
            status = await promo.check_bio(bot, user.id, user.ref_code, me.username or "")
            taken = await promo.apply_recheck(session, claim.id, status, now)
            if taken is None:
                continue
            revoked += 1
            await safe_send(bot, user.id, texts.PROFILE_REVOKED_USER.format(amount=fmt_rub(taken)) if taken > 0
                            else texts.PROFILE_REVOKED_ZERO_USER)
            await asyncio.sleep(SEND_PAUSE)
    return revoked


async def watch(bot: Bot, config: Config, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Каждые 15 минут: бонусы за друзей, напоминания об оплате, дедлайны, чеки без проверки, перепроверка профилей."""
    for name, job in (("канал", lambda: channel_watch(bot, sessionmaker)),
                      ("оплата", lambda: remind_unpaid(bot, sessionmaker)),
                      ("дедлайны", lambda: alert_deadlines(bot, config, sessionmaker)),
                      ("чеки", lambda: remind_receipts(bot, config, sessionmaker)),
                      ("профили", lambda: profile_recheck(bot, sessionmaker))):
        try:
            await job()
        except Exception:  # noqa: BLE001 — одна упавшая часть не мешает остальным
            log.exception("watch: часть «%s» упала", name)


async def admin_digest(bot: Bot, config: Config, sessionmaker: async_sessionmaker[AsyncSession],
                       now: datetime | None = None) -> bool:
    """09:00 — сводка для команды: что ждёт действий. Если ничего — не пишем."""
    now = now or now_utc()
    async with sessionmaker() as session:
        counts = await svc.status_counts(session)
        open_tickets = await tickets.open_count(session)
        receipts = len(await manual_queue(session, 100))
        task_count = await promo.pending_total(session)
        nearest = await svc.nearest_deadline(session, now)
    parts = [("🔎 на проверке", counts.get("review", 0)), ("✅ в очереди", counts.get("paid", 0)),
             ("🛠 в работе", counts.get("in_work", 0)), ("⚠️ споры", counts.get("dispute", 0)),
             ("💳 ждут оплаты", counts.get("awaiting_payment", 0)), ("🧾 чеки", receipts),
             ("📨 обращения", open_tickets), ("🎯 задания ждут проверки", task_count)]
    if not any(n for label, n in parts if label != "💳 ждут оплаты"):
        return False
    lines = ["☀️ <b>Доброе утро! Сводка на сегодня</b>"]
    lines += [f"{label}: <b>{n}</b>" for label, n in parts if n]
    if nearest is not None and nearest.deadline_at is not None:
        lines.append(f"\n⏰ Ближайший дедлайн: #{nearest.id} — {fmt_dt(nearest.deadline_at)} "
                     f"({fmt_left(nearest.deadline_at, now)})")
    await notify_admins(bot, config, "\n".join(lines),
                        reply_markup=ik([cb("🛡 Открыть админку", AdminCb(s="m"))]))
    return True


async def drafts_cleanup(bot: Bot, config: Config, sessionmaker: async_sessionmaker[AsyncSession],
                         now: datetime | None = None) -> tuple[int, int]:
    """Раз в час: черновик без движения draft_ttl_days дней удаляем, а за день до этого предупреждаем
    (с кнопкой «▶️ Продолжить»). Без предупреждения черновик с фото или текстом не удаляется никогда: если бот
    был выключен или черновик остался с 2.2 и уже старше срока — сначала предупреждаем, удаляем через сутки.
    Пустые черновики (без фото и текста) удаляем молча. → (предупредили, удалили)."""
    now = now or now_utc()
    warned, deleted, notes = 0, 0, []
    day = timedelta(days=1)
    async with sessionmaker() as session:
        s = await load_settings(session)
        days = max(int(s.draft_ttl_days), 1)
        ttl = timedelta(days=days)
        drafts_ = (await session.execute(select(Order).where(Order.status == "draft"))).scalars().all()
        for order in drafts_:
            last_file = await session.scalar(select(func.max(OrderFile.created_at))
                                             .where(OrderFile.order_id == order.id))
            last = last_activity(order, last_file)
            n = await svc.count_files(session, order.id, "condition")
            meaningful = bool(n or (order.comment or "").strip()
                              or await session.scalar(select(func.count(OrderFile.id))
                                                      .where(OrderFile.order_id == order.id)))
            warned_at = order.draft_warned_at if order.draft_warned_at and order.draft_warned_at >= last else None
            if not meaningful or warned_at is not None:
                if now >= max(last + ttl, warned_at + day if warned_at else last):
                    user_id = order.user_id
                    await svc.delete_draft(session, order, config.uploads_dir)
                    deleted += 1
                    if meaningful:
                        notes.append((user_id, texts.DRAFT_EXPIRED.format(days=max((now - last).days, days)),
                                      None))
            elif now >= last + ttl - day:
                # updated_at не трогаем: предупреждение — не «движение» по черновику, срок не сдвигается
                await session.execute(update(Order).where(Order.id == order.id)
                                      .values(draft_warned_at=now, updated_at=Order.updated_at))
                await session.commit()
                warned += 1
                notes.append((order.user_id, texts.DRAFT_WARN.format(n=n, days=max((now - last).days, 1)),
                              ik([cb(texts.DRAFT_CONTINUE, WizCb(action="resume"))])))
    for user_id, text, kb in notes:
        await safe_send(bot, user_id, text,
                        reply_markup=kb or main_menu(config.admin_level(user_id) > 0, draft=None))
        await asyncio.sleep(SEND_PAUSE)
    return warned, deleted


async def unlimited_watch(bot: Bot, config: Config, sessionmaker: async_sessionmaker[AsyncSession],
                          now: datetime | None = None) -> tuple[int, int]:
    """Раз в час: за 3 дня до конца 30-дневного безлимита — одно напоминание; закончился — снимаем и сообщаем.
    → (напомнили, закончились)."""
    now = now or now_utc()
    async with sessionmaker() as session:
        due = await promocodes.due_reminders(session, now)
        reminders = [(u.id, u.unlimited_until) for u in due]
        for u in due:
            u.unlimited_reminded_at = now
        await session.commit()
        ended = await promocodes.expire(session, now)
    for user_id, until in reminders:
        await safe_send(bot, user_id, texts.PROMO_REMIND.format(date=full_date(until),
                                                                left=fmt_left(until, now).removeprefix("через ")))
        await asyncio.sleep(SEND_PAUSE)
    for user_id, _until in ended:
        await safe_send(bot, user_id, texts.PROMO_ENDED)
        await asyncio.sleep(SEND_PAUSE)
    if ended:
        log.info("безлимит закончился у %s пользователей", len(ended))
    return len(reminders), len(ended)


async def daily_backup(bot: Bot, config: Config) -> None:
    path = await backup.make_backup(config)
    log.info("автобэкап готов: %s", path)


def _guarded(name: str, bot: Bot, config: Config, fn: Callable[[], Awaitable[object]]) -> Callable[[], Awaitable[None]]:
    async def job() -> None:
        try:
            await fn()
        except Exception as e:  # noqa: BLE001
            log.exception("фоновая задача %s упала", name)
            await notify_admins(bot, config, texts.ADMIN_ERROR.format(
                error=html.escape(f"фоновая задача «{name}»: {type(e).__name__}: {e}"[:600])), min_level=2)

    return job


def setup_scheduler(bot: Bot, config: Config, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)
    opts = {"misfire_grace_time": 3600, "coalesce": True, "max_instances": 1, "replace_existing": True}
    jobs = [
        ("бэкап", "backup", CronTrigger(hour=config.backup_hour % 24, minute=0, timezone=TZ),
         lambda: daily_backup(bot, config)),
        ("сгорание бонусов", "burn", CronTrigger(hour=0, minute=5, timezone=TZ),
         lambda: burn_bonuses(bot, sessionmaker)),
        ("напоминание о бонусах", "bonus_reminders", CronTrigger(hour=12, minute=0, timezone=TZ),
         lambda: remind_bonuses(bot, sessionmaker)),
        ("напоминание «приём открыт»", "remind_open", CronTrigger(hour=9, minute=0, timezone=TZ),
         lambda: remind_open(bot, sessionmaker)),
        ("сводка для команды", "admin_digest", CronTrigger(hour=9, minute=0, timezone=TZ),
         lambda: admin_digest(bot, config, sessionmaker)),
        ("наблюдение (канал, оплаты, дедлайны, чеки, профили)", "watch", IntervalTrigger(minutes=15, timezone=TZ),
         lambda: watch(bot, config, sessionmaker)),
        ("«Это задание?» без ответа → в поддержку", "stray", IntervalTrigger(minutes=1, timezone=TZ),
         lambda: flush_stray(bot, config, sessionmaker)),
        ("черновики заказов", "drafts", IntervalTrigger(hours=1, timezone=TZ),
         lambda: drafts_cleanup(bot, config, sessionmaker)),
        ("безлимит: напоминания и окончание", "unlimited", IntervalTrigger(hours=1, timezone=TZ),
         lambda: unlimited_watch(bot, config, sessionmaker)),
    ]
    for name, job_id, trigger, fn in jobs:
        scheduler.add_job(_guarded(name, bot, config, fn), trigger, id=job_id, name=name, **opts)
    return scheduler
