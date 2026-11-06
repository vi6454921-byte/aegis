"""Статистика для админа и счётчик доверия для приветствия. Всё - из БД."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.db.models import (Order, Payment, ProfileClaim, PromoCode, PromoPost, PromoPostReport, Referral, Ticket,
                           Transaction, User)
from app.db.sums import total as sum_of
from app.services.promo import TASK_KINDS
from app.utils.money import fmt_rub, plural
from app.utils.timeutil import local_day_bounds, local_today


async def trust_numbers(session: AsyncSession, s) -> tuple[int, int]:  # noqa: ANN001
    """(сдано работ, % с первой попытки) = стартовые цифры из настроек + реальные из БД (без тестовых)."""
    finalized = int(await session.scalar(select(func.count(Order.id)).where(
        Order.status.in_(("accepted", "refunded")), Order.done_at.is_not(None), Order.is_test.is_(False))) or 0)
    first_try = int(await session.scalar(select(func.count(Order.id)).where(
        Order.status == "accepted", Order.had_dispute.is_(False), Order.is_test.is_(False))) or 0)
    done = int(s.stats_offset_done) + finalized
    ok = int(s.stats_offset_first_try) + first_try
    pct = round(100 * ok / done) if done else 0
    return done, pct


def trust_line(done: int, pct: int) -> str:
    verb = "Сдана" if done % 10 == 1 and done % 100 != 11 else "Сдано"
    return f"{verb} {done} {plural(done, ('работа', 'работы', 'работ'))}, {pct}% с первой попытки"


async def _count(session: AsyncSession, q) -> int:  # noqa: ANN001
    return int(await session.scalar(q) or 0)


async def admin_report(session: AsyncSession, now: datetime) -> str:
    today_start, _ = local_day_bounds(local_today(now))
    week_start = today_start - timedelta(days=6)
    lines = ["📊 <b>Статистика</b>"]

    total = await _count(session, select(func.count(User.id)))
    today = await _count(session, select(func.count(User.id)).where(User.created_at >= today_start))
    week = await _count(session, select(func.count(User.id)).where(User.created_at >= week_start))
    blocked = await _count(session, select(func.count(User.id)).where(User.bot_blocked.is_(True)))
    lines.append(f"\n👥 Пользователи: всего <b>{total}</b> · сегодня {today} · за 7 дней {week}"
                 f" · заблокировали бота {blocked}")

    src = dict((await session.execute(select(User.source, func.count(User.id)).group_by(User.source))).all())
    lines.append(f"Источники: рефералы {src.get('ref', 0)} · метки src_ {src.get('src', 0)}"
                 f" · прямые {src.get('direct', 0)}")
    tags = (await session.execute(select(User.source_tag, func.count(User.id)).where(User.source == "src")
                                  .group_by(User.source_tag).order_by(func.count(User.id).desc()).limit(10))).all()
    if tags:
        lines.append("Метки: " + ", ".join(f"{t}: {n}" for t, n in tags))

    by_status = dict((await session.execute(select(Order.status, func.count(Order.id))
                                            .where(Order.status != "draft").group_by(Order.status))).all())
    lines.append("\n📦 Заказы: " + (", ".join(f"{texts.STATUS[k]} {v}" for k, v in by_status.items()
                                             if k in texts.STATUS) or "пока нет"))

    lines.append("\n💰 Деньги (реальные | тестовые):")
    for title, cond in (("Поступило по счетам", and_(Payment.status.in_(("paid", "refunded")),
                                                      Payment.provider.in_(("stars", "test", "manual")))),
                        ("Возвращено звёздами", Payment.refund_method == "stars")):
        vals = []
        for is_test in (False, True):
            vals.append(fmt_rub(await sum_of(session, Payment.amount_rub, cond, Payment.is_test.is_(is_test))))
        lines.append(f"• {title}: {vals[0]} | {vals[1]}")
    for title, kind, account in (("Оплачено заказов рублями", "order_pay", "rub"),
                                 ("Оплачено заказов бонусами", "order_pay", "bonus"),
                                 ("Возвраты на баланс", "refund", "rub")):
        vals = []
        for is_test in (False, True):
            v = await sum_of(session, Transaction.amount, Transaction.kind == kind, Transaction.account == account,
                             Transaction.is_test.is_(is_test))
            vals.append(fmt_rub(abs(v)))
        lines.append(f"• {title}: {vals[0]} | {vals[1]}")
    rub_total = await sum_of(session, User.balance_rub)
    bonus_total = await sum_of(session, User.balance_bonus)
    lines.append(f"• Сейчас на балансах пользователей: рублями {fmt_rub(rub_total)}, бонусами {fmt_rub(bonus_total)}")

    ordered = await _count(session, select(func.count(distinct(Order.user_id))).where(
        Order.submitted_at.is_not(None)))
    # заказы по безлимиту (промокод, 0 ₽) - не оплата: считаем их отдельно ниже
    paid_q = select(Order.user_id, func.count(Order.id).label("n")).where(Order.paid_at.is_not(None),
                                                                          Order.unlimited.is_(False)) \
        .group_by(Order.user_id).subquery()
    paid_users = await _count(session, select(func.count()).select_from(paid_q))
    repeat = await _count(session, select(func.count()).select_from(paid_q).where(paid_q.c.n >= 2))

    def pct(a: int, b: int) -> str:
        return f"{round(100 * a / b)}%" if b else "-"

    lines.append(f"\n🔻 Воронка: вход {total} → заказ {ordered} ({pct(ordered, total)}) → оплата {paid_users} "
                 f"({pct(paid_users, ordered)}) → повторная покупка {repeat} ({pct(repeat, paid_users)})")

    finalized = await _count(session, select(func.count(Order.id)).where(
        Order.status.in_(("accepted", "refunded")), Order.done_at.is_not(None), Order.is_test.is_(False)))
    first = await _count(session, select(func.count(Order.id)).where(
        Order.status == "accepted", Order.had_dispute.is_(False), Order.is_test.is_(False)))
    lines.append(f"🎓 Зачтено с первой попытки (только БД, без тестовых): {first} из {finalized}, {pct(first, finalized)}")

    explain = await _count(session, select(func.count(Order.id)).where(Order.subject == "explain",
                                                                      Order.status != "draft"))
    if explain:
        lines.append(f"📸 Заявок на объяснение материала: {explain}")
    lines.append(await unlimited_line(session, now))
    phones = await _count(session, select(func.count(User.id)).where(User.phone.is_not(None)))
    lines.append(f"📱 Оставили номер телефона: {phones}")

    subs = await _count(session, select(func.count(User.id)).where(User.channel_bonus_at.is_not(None)))
    revoked = await _count(session, select(func.count(User.id)).where(User.channel_bonus_revoked_at.is_not(None)))
    members = await _count(session, select(func.count(User.id)).where(User.channel_status == "member"))
    ref_sub = dict((await session.execute(select(Referral.sub_status, func.count(Referral.id))
                                          .group_by(Referral.sub_status))).all())
    lines.append(f"\n📢 Канал: подписаны (по данным бота) {members} · бонус за подписку получили {subs}"
                 f" · списан за отписку {revoked}")
    lines.append(f"👥 Друзья по ссылкам: бонус выдан {ref_sub.get('rewarded', 0)} · ждут удержания "
                 f"{ref_sub.get('pending', 0)} · отменено {ref_sub.get('cancelled', 0)}")

    prof = dict((await session.execute(select(ProfileClaim.status, func.count(ProfileClaim.id))
                                       .group_by(ProfileClaim.status))).all())
    posts = dict((await session.execute(select(PromoPost.status, func.count(PromoPost.id))
                                        .group_by(PromoPost.status))).all())
    reports_wait = await _count(session, select(func.count(PromoPostReport.id)).where(
        PromoPostReport.status == "pending"))
    task_money = await sum_of(session, Transaction.amount, Transaction.kind.in_(TASK_KINDS))
    lines.append(f"\n📌 Ссылка в профиле: засчитано {prof.get('approved', 0)} · на проверке {prof.get('pending', 0)}"
                 f" · снято при перепроверке {prof.get('revoked', 0)}")
    lines.append(f"📣 Посты о нас: принято {posts.get('accepted', 0)} · удалено {posts.get('deleted', 0)}"
                 f" · отчётов ждут проверки {reports_wait}")
    lines.append(f"🎁 За задания начислено (за вычетом списаний): {fmt_rub(task_money)}")

    open_t = await _count(session, select(func.count(Ticket.id)).where(Ticket.status == "open"))
    manual_wait = await _count(session, select(func.count(Payment.id)).where(
        Payment.provider == "manual", Payment.claimed_at.is_not(None), Payment.reviewed_at.is_(None),
        Payment.status.in_(("pending", "cancelled"))))  # как в payments.manual_queue
    lines.append(f"\n📨 Открытых обращений: {open_t} · 🧾 чеков ждут проверки: {manual_wait}")
    return "\n".join(lines)


async def unlimited_line(session: AsyncSession, now: datetime) -> str:
    """Безлимит по промокодам - отдельно от выручки: такие заказы бесплатные (0 ₽)."""
    forever = await _count(session, select(func.count(User.id)).where(User.unlimited_forever.is_(True)))
    dated = await _count(session, select(func.count(User.id)).where(User.unlimited_forever.is_(False),
                                                                    User.unlimited_until > now))
    orders = await _count(session, select(func.count(Order.id)).where(Order.unlimited.is_(True),
                                                                      Order.status != "draft"))
    queued = await _count(session, select(func.count(Order.id)).where(Order.unlimited.is_(True),
                                                                      Order.paid_at.is_not(None)))
    state = case((PromoCode.revoked_at.is_not(None), "revoked"), (PromoCode.used_by.is_not(None), "used"),
                 else_="free").label("state")
    codes = dict((await session.execute(select(state, func.count(PromoCode.id)).group_by(state))).all())
    return texts.STATS_UNLIMITED.format(active=forever + dated, forever=forever, dated=dated, orders=orders,
                                        queued=queued, free=codes.get("free", 0), used=codes.get("used", 0),
                                        revoked=codes.get("revoked", 0))
