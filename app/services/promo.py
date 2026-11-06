"""Задания v2.2: «📌 Ссылка в профиле» и «📣 Пост о нас». Награды - обычные рубли на балансе.

Все начисления и списания - через журнал (ledger) под app_lock, решения админов - в admin_log.
• Профиль: скрин → проверка админом → награда один раз на аккаунт. Пока заявка на проверке - новую не принимаем,
  после отказа - можно снова. Перепроверка через N дней: ссылки в «О себе» нет - награда списывается (не ниже нуля).
• Пост: ссылка + скрин → «Принять» (награда за пост) → отчёты о просмотрах: накопительно,
  всего = floor(просмотры / шаг) × сумма за шаг (не больше потолка), к выплате = всего − уже выплачено за просмотры.
  Одна ссылка (после нормализации) - один раз во всей системе.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import unquote, urlsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.db.models import AdminLog, ProfileClaim, PromoPost, PromoPostReport, User
from app.services.admin_log import log_admin
from app.services.ledger import credit_rub, debit_rub, lock_user
from app.services.locks import app_lock
from app.utils.money import fits_int64, fmt_rub
from app.utils.timeutil import fmt_dt

# операции журнала по заданиям («📌 Ссылка в профиле», «📣 Пост о нас») - для отчётов и статистики
TASK_KINDS = ("profile_link", "profile_link_revoke", "promo_post", "promo_views", "promo_adjust")

SYSTEM = 0  # admin_id в журнале для решений самого бота (перепроверка профиля)
POST_ACTIVE = ("pending", "accepted")
MAX_VIEWS = 10**12


class PromoError(Exception):
    """Текст для человека."""


def ref_link(bot_username: str, user: User) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user.ref_code}"


def render_template(template: str, link: str) -> str:
    return (template or texts.DEFAULT_POST_TEMPLATE).replace("{link}", link)


# ------------------------------------------------------------------ ссылки на посты
_TG_RE = re.compile(r"^(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)$", re.IGNORECASE)
_TG_PATH_RE = re.compile(r"^/(?:s/)?(c/\d+|[A-Za-z][A-Za-z0-9_]{3,31})/(\d+)(?:/(\d+))?/?$")
_VK_HOSTS = {"vk.com", "m.vk.com", "www.vk.com", "vk.ru", "m.vk.ru", "www.vk.ru", "vkontakte.ru"}
_VK_ITEM_RE = re.compile(r"(wall|clip|video|photo)(-?\d+_\d+)")


def _split(text: str):  # noqa: ANN202
    t = (text or "").strip().split()[0] if (text or "").strip() else ""
    if not t:
        return None
    if not re.match(r"^https?://", t, re.IGNORECASE):
        t = "https://" + t
    try:
        return urlsplit(t)
    except ValueError:
        return None


def normalize_tg(text: str) -> str | None:
    """t.me/name/123, t.me/s/name/123, telegram.me/name/123?single, t.me/c/123/45 → https://t.me/name/123."""
    u = _split(text)
    if u is None or not _TG_RE.match(u.hostname or ""):
        return None
    m = _TG_PATH_RE.match(u.path)
    if not m:
        return None
    name, post, sub = m.group(1), m.group(2), m.group(3)
    name = name.lower() if not name.startswith("c/") else name
    return f"https://t.me/{name}/{post}" + (f"/{sub}" if sub else "")


def normalize_vk(text: str) -> str | None:
    """vk.com/wall-1_2, m.vk.com/wall-1_2?from=…, vk.com/club1?w=wall-1_2, vk.ru/clip-1_2 → https://vk.com/wall-1_2."""
    u = _split(text)
    if u is None or (u.hostname or "").lower() not in _VK_HOSTS:
        return None
    for part in (u.path, unquote(u.query), unquote(u.fragment)):
        m = _VK_ITEM_RE.search(part or "")
        if m:
            return f"https://vk.com/{m.group(1)}{m.group(2)}"
    return None


def normalize_post_url(text: str) -> tuple[str, str] | None:
    """(платформа, нормализованная ссылка) или None. Платформу определяем по самой ссылке."""
    if url := normalize_tg(text):
        return "tg", url
    if url := normalize_vk(text):
        return "vk", url
    return None


# ------------------------------------------------------------------ «📌 Ссылка в профиле»
def bio_status(bio: str | None, ref_code: str, bot_username: str) -> str:
    """found - реф-ссылка в «О себе»; bot_only - ссылка на бота без реф-кода; missing - ничего."""
    text = (bio or "").lower()
    if ref_code and f"ref_{ref_code}".lower() in text:
        return "found"
    if bot_username and bot_username.lower().lstrip("@") in text:
        return "bot_only"
    return "missing"


async def check_bio(bot, user_id: int, ref_code: str, bot_username: str) -> str:  # noqa: ANN001
    """Подсказка админу: Telegram отдаёт «О себе» через getChat. Ошибка - unknown (решает админ по скрину)."""
    try:
        chat = await bot.get_chat(user_id)
    except Exception:  # noqa: BLE001
        return "unknown"
    return bio_status(getattr(chat, "bio", None), ref_code, bot_username)


async def latest_claim(session: AsyncSession, user_id: int) -> ProfileClaim | None:
    return await session.scalar(select(ProfileClaim).where(ProfileClaim.user_id == user_id)
                                .order_by(ProfileClaim.id.desc()).limit(1)
                                .execution_options(populate_existing=True))


async def profile_done(session: AsyncSession, user_id: int) -> ProfileClaim | None:
    """Задание уже засчитано (или награду списали при перепроверке) - повторно не начисляется."""
    return await session.scalar(select(ProfileClaim).where(
        ProfileClaim.user_id == user_id, ProfileClaim.status.in_(("approved", "revoked"))).limit(1))


async def profile_block(session: AsyncSession, user_id: int, s) -> str | None:  # noqa: ANN001
    """Почему нельзя отправить заявку сейчас (текст для пользователя) или None."""
    done = await profile_done(session, user_id)
    if done is not None:
        if done.status == "revoked":
            return texts.PROFILE_REVOKED.format(date=fmt_dt(done.revoked_at))
        return texts.PROFILE_DONE.format(reward=fmt_rub(done.reward), date=fmt_dt(done.reviewed_at))
    if not s.profile_task_enabled:
        return texts.PROFILE_DISABLED
    pending = await session.scalar(select(ProfileClaim).where(ProfileClaim.user_id == user_id,
                                                              ProfileClaim.status == "pending").limit(1))
    if pending is not None:
        return texts.PROFILE_PENDING.format(date=fmt_dt(pending.created_at))
    return None


async def submit_profile(session: AsyncSession, user: User, file_id: str, file_type: str, link: str,
                         bio: str, s, now: datetime) -> ProfileClaim:  # noqa: ANN001
    async with app_lock():
        block = await profile_block(session, user.id, s)
        if block:
            raise PromoError(block)
        claim = ProfileClaim(user_id=user.id, status="pending", link=link, file_id=file_id, file_type=file_type,
                             bio_check=bio, created_at=now)
        session.add(claim)
        await session.commit()
        return claim


async def approve_profile(session: AsyncSession, claim_id: int, admin_id: int, s, now: datetime) -> ProfileClaim:  # noqa: ANN001
    async with app_lock():
        claim = await session.get(ProfileClaim, claim_id, populate_existing=True)
        if claim is None:
            raise PromoError(texts.TASK_NOT_FOUND)
        if claim.status != "pending":
            raise PromoError(texts.TASK_ALREADY.format(status=texts.PROFILE_STATUS.get(claim.status, claim.status)))
        user = await lock_user(session, claim.user_id)
        if user.is_banned:
            raise PromoError(texts.TASK_BANNED)
        if await profile_done(session, user.id) is not None:
            claim.status, claim.reject_reason = "rejected", texts.PROFILE_ALREADY_REASON
            claim.reviewer_id, claim.reviewed_at = admin_id, now
            await session.commit()
            raise PromoError(texts.TASK_ALREADY_REWARDED)
        reward = int(s.profile_task_reward)
        if reward > 0 and not fits_int64(user.balance_rub + reward):
            raise PromoError(texts.TASK_OVERFLOW)
        if reward > 0:
            await credit_rub(session, user, reward, "profile_link", admin_id=admin_id, comment=f"заявка №{claim.id}")
        claim.status, claim.reward, claim.reviewer_id, claim.reviewed_at = "approved", reward, admin_id, now
        log_admin(session, admin_id, "profile_approve", "profile_claim", claim.id, user=user.id, amount=reward)
        await session.commit()
        return claim


async def reject_profile(session: AsyncSession, claim_id: int, admin_id: int, reason: str | None,
                         now: datetime) -> ProfileClaim:
    async with app_lock():
        claim = await session.get(ProfileClaim, claim_id, populate_existing=True)
        if claim is None:
            raise PromoError(texts.TASK_NOT_FOUND)
        if claim.status != "pending":
            raise PromoError(texts.TASK_ALREADY.format(status=texts.PROFILE_STATUS.get(claim.status, claim.status)))
        claim.status, claim.reject_reason = "rejected", (reason or None)
        claim.reviewer_id, claim.reviewed_at = admin_id, now
        log_admin(session, admin_id, "profile_reject", "profile_claim", claim.id, user=claim.user_id, reason=reason)
        await session.commit()
        return claim


async def recheck_due(session: AsyncSession, s, now: datetime) -> list[ProfileClaim]:  # noqa: ANN001
    """Засчитанные заявки, которые пора перепроверить (profile_recheck_days после одобрения; 0 - выкл)."""
    days = int(s.profile_recheck_days)
    if days <= 0:
        return []
    rows = (await session.execute(select(ProfileClaim).where(ProfileClaim.status == "approved",
                                                             ProfileClaim.rechecked_at.is_(None))
                                  .order_by(ProfileClaim.id).limit(500))).scalars().all()
    return [c for c in rows if c.reviewed_at is not None and c.reviewed_at + timedelta(days=days) <= now
            and (c.recheck_at is None or c.recheck_at <= now)]


async def apply_recheck(session: AsyncSession, claim_id: int, status: str, now: datetime) -> int | None:
    """Итог перепроверки. missing - списать награду (не больше баланса), вернуть списанное; иначе None."""
    async with app_lock():
        claim = await session.get(ProfileClaim, claim_id, populate_existing=True)
        if claim is None or claim.status != "approved" or claim.rechecked_at is not None:
            return None
        if status == "unknown":  # Telegram не ответил - попробуем завтра
            claim.recheck_at = now + timedelta(days=1)
            await session.commit()
            return None
        claim.bio_check = status
        if status in ("found", "bot_only"):
            claim.rechecked_at = now
            await session.commit()
            return None
        user = await lock_user(session, claim.user_id)
        take = min(int(claim.reward or 0), max(user.balance_rub, 0))
        if take > 0:
            await debit_rub(session, user, take, "profile_link_revoke", comment=f"заявка №{claim.id}")
        claim.status, claim.revoked_at, claim.revoked_amount, claim.rechecked_at = "revoked", now, take, now
        log_admin(session, SYSTEM, "profile_revoke", "profile_claim", claim.id, user=user.id, amount=take)
        await session.commit()
        return take


# ------------------------------------------------------------------ «📣 Пост о нас»
def views_total(views: int, s) -> int:  # noqa: ANN001
    """Сколько всего положено за просмотры: floor(views / шаг) × сумма за шаг, не больше потолка (0 - без)."""
    step = max(int(s.post_views_step), 1)
    total = (max(int(views), 0) // step) * int(s.post_views_reward)
    cap = int(s.post_views_cap)
    return min(total, cap) if cap > 0 else total


def next_views_goal(views: int, s) -> int:  # noqa: ANN001
    step = max(int(s.post_views_step), 1)
    return (max(int(views), 0) // step + 1) * step


async def user_posts(session: AsyncSession, user_id: int) -> list[PromoPost]:
    return list((await session.execute(select(PromoPost).where(PromoPost.user_id == user_id)
                                       .order_by(PromoPost.id.desc()).limit(30)
                                       .execution_options(populate_existing=True))).scalars())


async def active_count(session: AsyncSession, user_id: int) -> int:
    return int(await session.scalar(select(func.count(PromoPost.id)).where(
        PromoPost.user_id == user_id, PromoPost.status.in_(POST_ACTIVE))) or 0)


async def post_block(session: AsyncSession, user_id: int, s) -> str | None:  # noqa: ANN001
    if not s.post_task_enabled:
        return texts.POST_DISABLED
    n = await active_count(session, user_id)
    if n >= int(s.post_max_active):
        return texts.POST_LIMIT.format(n=n)
    return None


async def check_url(session: AsyncSession, user_id: int, url: str) -> PromoPost | None:
    """Дубликаты: чужой пост - отказ; свой отклонённый - можно прислать снова (вернём ту же запись)."""
    existing = await session.scalar(select(PromoPost).where(PromoPost.post_url == url)
                                    .execution_options(populate_existing=True))
    if existing is None:
        return None
    if existing.user_id != user_id:
        raise PromoError(texts.POST_URL_DUP_OTHER)
    if existing.status == "rejected":
        return existing
    raise PromoError(texts.POST_URL_DUP_OWN.format(status=texts.POST_STATUS.get(existing.status, existing.status)))


async def submit_post(session: AsyncSession, user: User, platform: str, url: str, file_id: str, file_type: str,
                      s, now: datetime) -> tuple[PromoPost, PromoPostReport]:  # noqa: ANN001
    async with app_lock():
        block = await post_block(session, user.id, s)
        if block:
            raise PromoError(block)
        post = await check_url(session, user.id, url)
        if post is None:
            post = PromoPost(user_id=user.id, platform=platform, post_url=url, status="pending", created_at=now,
                             last_update_at=now, last_report_at=now)
            session.add(post)
        else:
            post.status, post.platform, post.reject_reason = "pending", platform, None
            post.reviewer_id = post.reviewed_at = None
            post.last_report_at = post.last_update_at = now
        try:
            await session.flush()
        except IntegrityError:  # ту же ссылку только что прислал другой аккаунт
            await session.rollback()
            raise PromoError(texts.POST_URL_DUP_OTHER) from None
        report = PromoPostReport(post_id=post.id, kind="first", status="pending", file_id=file_id,
                                 file_type=file_type, created_at=now)
        session.add(report)
        await session.commit()
        return post, report


async def views_block(session: AsyncSession, post: PromoPost, s, now: datetime) -> str | None:  # noqa: ANN001
    """Почему отчёт о просмотрах сейчас не принимается (текст для пользователя) или None."""
    if post.status != "accepted":
        return texts.VIEWS_CLOSED.format(status=texts.POST_STATUS.get(post.status, post.status))
    pending = int(await session.scalar(select(func.count(PromoPostReport.id)).where(
        PromoPostReport.post_id == post.id, PromoPostReport.status == "pending")) or 0)
    if pending >= int(s.post_max_pending):
        return texts.VIEWS_PENDING
    hours = int(s.post_report_cooldown_hours)
    if hours > 0 and post.last_report_at is not None and post.last_report_at + timedelta(hours=hours) > now:
        return texts.VIEWS_COOLDOWN.format(hours=hours, next=fmt_dt(post.last_report_at + timedelta(hours=hours)))
    return None


async def submit_views(session: AsyncSession, user_id: int, post_id: int, file_id: str, file_type: str,
                       claimed: int, s, now: datetime) -> tuple[PromoPost, PromoPostReport]:  # noqa: ANN001
    async with app_lock():
        post = await session.get(PromoPost, post_id, populate_existing=True)
        if post is None or post.user_id != user_id:
            raise PromoError(texts.NOT_FOUND)
        block = await views_block(session, post, s, now)
        if block:
            raise PromoError(block)
        if claimed <= post.verified_views:
            raise PromoError(texts.VIEWS_NOT_MORE.format(n=post.verified_views))
        report = PromoPostReport(post_id=post.id, kind="views", status="pending", file_id=file_id,
                                 file_type=file_type, claimed_views=claimed, created_at=now)
        session.add(report)
        post.last_report_at = post.last_update_at = now
        await session.commit()
        return post, report


async def _report(session: AsyncSession, report_id: int, kind: str) -> tuple[PromoPostReport, PromoPost]:
    report = await session.get(PromoPostReport, report_id, populate_existing=True)
    if report is None or report.kind != kind:
        raise PromoError(texts.TASK_NOT_FOUND)
    if report.status != "pending":
        raise PromoError(texts.TASK_ALREADY.format(status=texts.REPORT_STATUS.get(report.status, report.status)))
    post = await session.get(PromoPost, report.post_id, populate_existing=True)
    if post is None:
        raise PromoError(texts.TASK_NOT_FOUND)
    return report, post


async def approve_post(session: AsyncSession, report_id: int, admin_id: int, s,  # noqa: ANN001
                       now: datetime) -> tuple[PromoPost, PromoPostReport]:
    async with app_lock():
        report, post = await _report(session, report_id, "first")
        if post.status != "pending":
            raise PromoError(texts.TASK_ALREADY.format(status=texts.POST_STATUS.get(post.status, post.status)))
        user = await lock_user(session, post.user_id)
        if user.is_banned:
            raise PromoError(texts.TASK_BANNED)
        base = int(s.post_base_reward)
        if base > 0 and not fits_int64(user.balance_rub + base):
            raise PromoError(texts.TASK_OVERFLOW)
        if base > 0:
            await credit_rub(session, user, base, "promo_post", admin_id=admin_id, comment=f"пост №{post.id}")
        post.status, post.base_reward_paid = "accepted", base
        post.reviewer_id, post.reviewed_at, post.last_update_at = admin_id, now, now
        report.status, report.credited, report.reviewer_id, report.reviewed_at = "approved", base, admin_id, now
        log_admin(session, admin_id, "promo_post_accept", "promo_post", post.id, user=user.id, amount=base,
                  url=post.post_url)
        await session.commit()
        return post, report


@dataclass
class ViewsResult:
    post: PromoPost
    report: PromoPostReport
    pay: int
    total: int  # всего начислено за пост (с наградой за пост и корректировками)
    capped: bool


async def approve_views(session: AsyncSession, report_id: int, views: int, admin_id: int, s,  # noqa: ANN001
                        now: datetime) -> ViewsResult:
    async with app_lock():
        report, post = await _report(session, report_id, "views")
        if post.status != "accepted":
            raise PromoError(texts.VIEWS_POST_CLOSED.format(status=texts.POST_STATUS.get(post.status, post.status)))
        if views < 0 or views > MAX_VIEWS:
            raise PromoError(texts.VIEWS_ADMIN_BAD)
        if views < post.verified_views:
            raise PromoError(texts.VIEWS_ADMIN_LESS.format(n=post.verified_views))
        user = await lock_user(session, post.user_id)
        if user.is_banned:
            raise PromoError(texts.TASK_BANNED)
        due = views_total(views, s)
        pay = max(due - int(post.views_reward_paid or 0), 0)
        if pay > 0 and not fits_int64(user.balance_rub + pay):
            raise PromoError(texts.TASK_OVERFLOW)
        if pay > 0:
            await credit_rub(session, user, pay, "promo_views", admin_id=admin_id,
                             comment=f"пост №{post.id}: {views} просмотров")
        post.verified_views = max(int(post.verified_views or 0), views)
        post.views_reward_paid = int(post.views_reward_paid or 0) + pay
        post.last_update_at = now
        report.status, report.verified_views, report.credited = "approved", views, pay
        report.reviewer_id, report.reviewed_at = admin_id, now
        log_admin(session, admin_id, "promo_views_approve", "promo_post", post.id, report=report.id, views=views,
                  claimed=report.claimed_views, amount=pay)
        await session.commit()
        cap = int(s.post_views_cap)
        return ViewsResult(post, report, pay, post.credited, cap > 0 and post.views_reward_paid >= cap)


async def reject_report(session: AsyncSession, report_id: int, admin_id: int, reason: str | None,
                        now: datetime) -> tuple[PromoPost, PromoPostReport]:
    """Отклонить первый скрин (пост целиком) или отчёт о просмотрах."""
    async with app_lock():
        report = await session.get(PromoPostReport, report_id, populate_existing=True)
        if report is None:
            raise PromoError(texts.TASK_NOT_FOUND)
        report, post = await _report(session, report_id, report.kind)
        report.status, report.reject_reason = "rejected", (reason or None)
        report.reviewer_id, report.reviewed_at = admin_id, now
        if report.kind == "first" and post.status == "pending":
            post.status, post.reject_reason = "rejected", (reason or None)
            post.reviewer_id, post.reviewed_at = admin_id, now
        post.last_update_at = now
        log_admin(session, admin_id, "promo_reject", "promo_post", post.id, report=report.id, kind=report.kind,
                  reason=reason)
        await session.commit()
        return post, report


async def mark_deleted(session: AsyncSession, post_id: int, admin_id: int,
                       now: datetime) -> tuple[PromoPost, list[int]]:
    """«🗑 Пост удалён»: новые отчёты не принимаются, ожидающие закрываются. Начисленное не трогаем."""
    async with app_lock():
        post = await session.get(PromoPost, post_id, populate_existing=True)
        if post is None:
            raise PromoError(texts.TASK_NOT_FOUND)
        if post.status not in POST_ACTIVE:
            raise PromoError(texts.POST_NOTHING_TO_DELETE.format(status=texts.POST_STATUS.get(post.status,
                                                                                              post.status)))
        pending = (await session.execute(select(PromoPostReport).where(
            PromoPostReport.post_id == post.id, PromoPostReport.status == "pending"))).scalars().all()
        for r in pending:
            r.status, r.reject_reason = "rejected", texts.POST_DELETED_REASON
            r.reviewer_id, r.reviewed_at = admin_id, now
        post.status, post.deleted_at, post.deleted_by, post.last_update_at = "deleted", now, admin_id, now
        log_admin(session, admin_id, "promo_post_deleted", "promo_post", post.id, url=post.post_url,
                  closed_reports=[r.id for r in pending])
        await session.commit()
        return post, [r.id for r in pending]


async def adjust_post(session: AsyncSession, post_id: int, amount: int, comment: str | None, admin_id: int,
                      op: str, now: datetime) -> tuple[PromoPost, int]:
    """Ручная корректировка по посту (только 2 уровень): + начислить, − списать (не ниже нуля). Идемпотентно по op."""
    comment = (comment or "").strip()[:300] or None
    async with app_lock():
        post = await session.get(PromoPost, post_id, populate_existing=True)
        if post is None:
            raise PromoError(texts.TASK_NOT_FOUND)
        if await session.scalar(select(AdminLog.id).where(AdminLog.admin_id == admin_id,
                                                          AdminLog.action == "promo_adjust",
                                                          AdminLog.details.like(f'%"op": "{op}"%')).limit(1)):
            return post, 0
        user = await lock_user(session, post.user_id)
        if amount > 0:
            if not fits_int64(amount, user.balance_rub + amount, post.adjust_total + amount):
                raise PromoError(texts.TASK_OVERFLOW)
            await credit_rub(session, user, amount, "promo_adjust", admin_id=admin_id,
                             comment=comment or f"пост №{post.id}")
            applied = amount
        else:
            take = min(-amount, user.balance_rub)
            if take <= 0:
                raise PromoError(texts.GIVE_NOTHING)
            await debit_rub(session, user, take, "promo_adjust", admin_id=admin_id,
                            comment=comment or f"пост №{post.id}")
            applied = -take
        post.adjust_total = int(post.adjust_total or 0) + applied
        post.last_update_at = now
        log_admin(session, admin_id, "promo_adjust", "promo_post", post.id, amount=applied, requested=amount,
                  comment=comment, op=op)
        await session.commit()
        return post, applied


# ------------------------------------------------------------------ для админки
async def pending_counts(session: AsyncSession) -> dict[str, int]:
    profile = int(await session.scalar(select(func.count(ProfileClaim.id))
                                       .where(ProfileClaim.status == "pending")) or 0)
    reports = dict((await session.execute(select(PromoPostReport.kind, func.count(PromoPostReport.id))
                                          .where(PromoPostReport.status == "pending")
                                          .group_by(PromoPostReport.kind))).all())
    return {"profile": profile, "post": int(reports.get("first", 0)), "views": int(reports.get("views", 0))}


async def pending_total(session: AsyncSession) -> int:
    return sum((await pending_counts(session)).values())


async def queue(session: AsyncSession, limit: int = 20) -> tuple[list[ProfileClaim], list[PromoPostReport]]:
    claims = list((await session.execute(select(ProfileClaim).where(ProfileClaim.status == "pending")
                                         .order_by(ProfileClaim.id).limit(limit))).scalars())
    reports = list((await session.execute(select(PromoPostReport).where(PromoPostReport.status == "pending")
                                          .order_by(PromoPostReport.id).limit(limit))).scalars())
    return claims, reports


async def post_reports(session: AsyncSession, post_id: int) -> list[PromoPostReport]:
    return list((await session.execute(select(PromoPostReport).where(PromoPostReport.post_id == post_id)
                                       .order_by(PromoPostReport.id))).scalars())


async def user_summary(session: AsyncSession, user_id: int) -> tuple[ProfileClaim | None, list[PromoPost]]:
    """Задания пользователя для карточки и отчёта: последняя заявка профиля и посты."""
    claim = await profile_done(session, user_id) or await latest_claim(session, user_id)
    return claim, await user_posts(session, user_id)
