"""Промокоды на безлимит (v2.3).

Кодов в репозитории нет (v2.4): их выпускает только админ 2 уровня в боте (🔐 Управление → 🎟 Промокоды),
файл с кодами приходит ему в чат. Файлы promo_codes*.txt в git и docker-образ не попадают.

Безлимит - все заказы бесплатно (решение и «📸 Объяснить материал»): счёт не выставляется, после проверки
админом заказ сразу встаёт в очередь как оплаченный (способ - «Промокод», 0 ₽). Деньги и бонусы не тратятся.

Код - одноразовый: после активации он навсегда привязан к одному аккаунту. Формат AEGIS-XXXX-XXXX, алфавит
без похожих символов (нет 0/O/1/I/L). Ввод не зависит от регистра, пробелов и дефисов, префикс можно не писать.

Один безлимит на аккаунт:
- действует «навсегда» → новый код не принимаем (и не тратим его);
- действует «на 30 дней» → код «навсегда» принимаем, он заменяет 30 дней; ещё один 30-дневный - нет.

Активация атомарна: под app_lock() и условным UPDATE (used_by IS NULL) с проверкой rowcount - один код не
активируют двое даже при одновременном вводе. Защита от перебора: 5 ошибок в час → пауза.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import actionlog, texts
from app.db.models import Order, Payment, PromoAttempt, PromoCode, User
from app.services.admin_log import log_admin
from app.services.locks import app_lock

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PREFIX = "AEGIS"
DAYS_DEFAULT = 30
MAX_FAILS = 5
FAIL_WINDOW = timedelta(hours=1)
REMIND_BEFORE = timedelta(days=3)
GENERATE_MAX = 200
_STRIP = re.compile(r"[\s\-_\u2013\u2014.]+")


def generate_code() -> str:
    part = lambda: "".join(secrets.choice(ALPHABET) for _ in range(4))  # noqa: E731
    return f"{PREFIX}-{part()}-{part()}"


def normalize(text: str | None) -> str | None:
    """«aegis abcd 2345», «ABCD2345», «AEGIS-ABCD-2345» → «AEGIS-ABCD-2345»; мусор → None."""
    raw = _STRIP.sub("", (text or "").upper())
    if raw.startswith(PREFIX) and len(raw) == len(PREFIX) + 8:
        raw = raw[len(PREFIX):]
    if len(raw) != 8 or any(ch not in ALPHABET for ch in raw):
        return None
    return f"{PREFIX}-{raw[:4]}-{raw[4:]}"


def looks_like_code(text: str | None) -> bool:
    return normalize(text) is not None


# ------------------------------------------------------------------ статус пользователя
def is_active(user: User | None, now: datetime) -> bool:
    return bool(user) and user.unlimited_active(now)


def kind_of(user: User, now: datetime) -> str | None:
    """forever | days | None"""
    if user.unlimited_forever:
        return "forever"
    if user.unlimited_until is not None and user.unlimited_until > now:
        return "days"
    return None


def _clear(user: User) -> None:
    user.unlimited_forever = False
    user.unlimited_until = None
    user.unlimited_since = None
    user.unlimited_code_id = None
    user.unlimited_by = None
    user.unlimited_reminded_at = None


async def _lock_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.scalar(select(User).where(User.id == user_id).with_for_update()
                                .execution_options(populate_existing=True))


async def _apply_to_orders(session: AsyncSession, user_id: int, now: datetime) -> list[int]:
    """Безлимит включился: заказы на проверке помечаем бесплатными, а ждущие оплаты - сразу в очередь (0 ₽).
    Возвращает id заказов, которые стали оплаченными. Без commit."""
    moved: list[int] = []
    rows = (await session.execute(select(Order).where(Order.user_id == user_id,
                                                      Order.status.in_(("review", "awaiting_payment")))
                                  .execution_options(populate_existing=True))).scalars().all()
    for order in rows:
        order.unlimited = True
        if order.status == "awaiting_payment":
            if order.free_tasks:  # «Месяц бесплатно» не тратим - возвращаем задачи
                user = await session.get(User, user_id)
                if user and user.free_month_until and user.free_month_until > now:
                    user.free_month_tasks_left += order.free_tasks
                order.free_tasks = 0
            await session.execute(update(Payment).where(Payment.order_id == order.id, Payment.status == "pending")
                                  .values(status="cancelled"))
            order.price_final = 0
            order.status = "paid"
            order.paid_at = now
            moved.append(order.id)
    return moved


async def _unflag_review(session: AsyncSession, user_id: int) -> None:
    """Безлимит сняли (отзыв/админ): заказы, которые ещё не приняты в очередь, снова платные. Без commit."""
    await session.execute(update(Order).where(Order.user_id == user_id, Order.status == "review",
                                              Order.unlimited.is_(True)).values(unlimited=False))


# ------------------------------------------------------------------ ввод кода
@dataclass
class Activation:
    status: str  # forever | days | blocked | invalid | used | mine | revoked | has_forever | has_days
    until: datetime | None = None
    wait_minutes: int = 0
    code: PromoCode | None = None
    paid_orders: list[int] | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("forever", "days")


async def fails_in_window(session: AsyncSession, user_id: int, now: datetime) -> tuple[int, datetime | None]:
    since = now - FAIL_WINDOW
    q = select(func.count(PromoAttempt.id), func.min(PromoAttempt.created_at)).where(
        PromoAttempt.user_id == user_id, PromoAttempt.ok.is_(False), PromoAttempt.created_at >= since)
    n, first = (await session.execute(q)).one()
    return int(n or 0), first


async def blocked_for(session: AsyncSession, user_id: int, now: datetime) -> int:
    """Сколько минут ещё пауза после 5 ошибок (0 - можно вводить)."""
    n, first = await fails_in_window(session, user_id, now)
    if n < MAX_FAILS or first is None:
        return 0
    left = first + FAIL_WINDOW - now
    return max(1, int(left.total_seconds() // 60) + (1 if left.total_seconds() % 60 else 0))


PROMO_RESULTS = {
    "invalid": "неверный код", "revoked": "код отозван", "used": "код уже использован", "mine": "уже активирован им",
    "blocked": "слишком много попыток", "has_forever": "уже есть безлимит навсегда",
    "has_days": "уже есть безлимит", "forever": "безлимит навсегда", "days": "безлимит на срок",
}


async def activate(session: AsyncSession, user_id: int, text: str, now: datetime) -> Activation:
    """Активирует промокод; в журнал действий попадает только маска кода (AEG-****-12) и итог."""
    res = await _activate(session, user_id, text, now)
    label = PROMO_RESULTS.get(res.status, res.status)
    if res.paid_orders:
        label += f", оплачено заказов: {len(res.paid_orders)}"
    actionlog.promo_applied(actionlog.by_id(user_id), normalize(text) or text, label)
    return res


async def _activate(session: AsyncSession, user_id: int, text: str, now: datetime) -> Activation:
    norm = normalize(text)
    async with app_lock():
        wait = await blocked_for(session, user_id, now)
        if wait:
            return Activation("blocked", wait_minutes=wait)

        def fail(status: str, code: PromoCode | None = None) -> Activation:
            session.add(PromoAttempt(user_id=user_id, code=(norm or (text or "").strip())[:64], ok=False,
                                     created_at=now))
            return Activation(status, code=code)

        code = await session.scalar(select(PromoCode).where(PromoCode.code == norm)
                                    .execution_options(populate_existing=True)) if norm else None
        if code is None:
            res = fail("invalid")
        elif code.revoked_at is not None:
            res = fail("revoked", code)
        elif code.used_by is not None:
            res = Activation("mine", code=code) if code.used_by == user_id else fail("used", code)
        else:
            res = None
        if res is not None:
            await session.commit()
            return res

        user = await _lock_user(session, user_id)
        current = kind_of(user, now)
        if current == "forever":  # код не тратим
            return Activation("has_forever", code=code)
        if current == "days" and code.kind != "forever":
            return Activation("has_days", until=user.unlimited_until, code=code)

        taken = await session.execute(update(PromoCode).where(PromoCode.id == code.id, PromoCode.used_by.is_(None),
                                                              PromoCode.revoked_at.is_(None))
                                      .values(used_by=user_id, used_at=now))
        if taken.rowcount != 1:  # кто-то успел раньше (другой процесс)
            await session.rollback()
            session.add(PromoAttempt(user_id=user_id, code=norm, ok=False, created_at=now))
            await session.commit()
            return Activation("used")
        if code.kind == "forever":
            user.unlimited_forever = True
            user.unlimited_until = None
        else:
            user.unlimited_forever = False
            user.unlimited_until = now + timedelta(days=code.days or DAYS_DEFAULT)
        user.unlimited_since = now
        user.unlimited_code_id = code.id
        user.unlimited_by = None
        user.unlimited_reminded_at = None
        session.add(PromoAttempt(user_id=user_id, code=norm, ok=True, created_at=now))
        moved = await _apply_to_orders(session, user_id, now)
        await session.commit()
        await session.refresh(code)
        return Activation("forever" if code.kind == "forever" else "days", until=user.unlimited_until, code=code,
                          paid_orders=moved)


# ------------------------------------------------------------------ админ
async def grant(session: AsyncSession, user_id: int, days: int | None, admin_id: int, now: datetime) -> User:
    """Выдать безлимит вручную: days=None - навсегда, иначе на N дней с этого момента."""
    async with app_lock():
        user = await _lock_user(session, user_id)
        if user is None:
            raise ValueError(texts.PC_USER_MISSING)
        user.unlimited_forever = days is None
        user.unlimited_until = None if days is None else now + timedelta(days=days)
        user.unlimited_since = now
        user.unlimited_code_id = None
        user.unlimited_by = admin_id
        user.unlimited_reminded_at = None
        moved = await _apply_to_orders(session, user_id, now)
        log_admin(session, admin_id, "unlimited_grant", "user", user_id, days=days or "forever", paid_orders=moved)
        await session.commit()
        return user


async def remove(session: AsyncSession, user_id: int, admin_id: int) -> User:
    async with app_lock():
        user = await _lock_user(session, user_id)
        if user is None:
            raise ValueError(texts.PC_USER_MISSING)
        log_admin(session, admin_id, "unlimited_remove", "user", user_id, forever=user.unlimited_forever,
                  until=user.unlimited_until, code_id=user.unlimited_code_id)
        _clear(user)
        await _unflag_review(session, user_id)
        await session.commit()
        return user


@dataclass
class Revoked:
    code: PromoCode
    user: User | None  # у кого сняли безлимит (если код был текущим источником)


async def revoke(session: AsyncSession, code_id: int, admin_id: int, now: datetime) -> Revoked:
    async with app_lock():
        code = await session.get(PromoCode, code_id, populate_existing=True)
        if code is None:
            raise ValueError(texts.PC_CODE_NOT_FOUND)
        if code.revoked_at is not None:
            raise ValueError(texts.PC_ALREADY_REVOKED)
        code.revoked_at = now
        code.revoked_by = admin_id
        affected = None
        if code.used_by is not None:
            user = await _lock_user(session, code.used_by)
            if user is not None and user.unlimited_code_id == code.id:
                _clear(user)
                await _unflag_review(session, user.id)
                affected = user
        log_admin(session, admin_id, "promo_revoke", "promo_code", code.id, code=code.code, used_by=code.used_by,
                  unlimited_removed=affected is not None)
        await session.commit()
        return Revoked(code, affected)


async def generate(session: AsyncSession, count: int, kind: str, days: int | None, admin_id: int,
                   now: datetime) -> list[PromoCode]:
    if not 1 <= count <= GENERATE_MAX:
        raise ValueError(texts.PC_GEN_COUNT_RANGE.format(max=GENERATE_MAX))
    if kind not in ("forever", "days"):
        raise ValueError(texts.PC_GEN_KIND_BAD)
    existing = set((await session.execute(select(PromoCode.code))).scalars().all())
    made: list[PromoCode] = []
    while len(made) < count:
        value = generate_code()
        if value in existing:
            continue
        existing.add(value)
        code = PromoCode(code=value, kind=kind, days=days if kind == "days" else None, created_at=now,
                         created_by=admin_id)
        session.add(code)
        made.append(code)
    await session.flush()
    log_admin(session, admin_id, "promo_generate", "promo_code", None, count=count, kind=kind, days=days,
              ids=[c.id for c in made])
    await session.commit()
    return made


async def all_codes(session: AsyncSession) -> list[PromoCode]:
    return list((await session.execute(select(PromoCode).order_by(PromoCode.id))).scalars().all())


async def page(session: AsyncSession, page_no: int, per_page: int, flt: str = "all") -> tuple[list[PromoCode], int]:
    q = select(PromoCode)
    if flt == "free":
        q = q.where(PromoCode.used_by.is_(None), PromoCode.revoked_at.is_(None))
    elif flt == "used":
        q = q.where(PromoCode.used_by.is_not(None), PromoCode.revoked_at.is_(None))
    elif flt == "revoked":
        q = q.where(PromoCode.revoked_at.is_not(None))
    total = int(await session.scalar(select(func.count()).select_from(q.subquery())) or 0)
    rows = (await session.execute(q.order_by(PromoCode.id).offset(page_no * per_page).limit(per_page))).scalars()
    return list(rows), total


async def counts(session: AsyncSession) -> dict[str, int]:
    codes = await all_codes(session)
    return {"all": len(codes),
            "free": sum(1 for c in codes if c.used_by is None and c.revoked_at is None),
            "used": sum(1 for c in codes if c.used_by is not None and c.revoked_at is None),
            "revoked": sum(1 for c in codes if c.revoked_at is not None)}


async def find(session: AsyncSession, text: str | None) -> PromoCode | None:
    norm = normalize(text)
    return await session.scalar(select(PromoCode).where(PromoCode.code == norm)) if norm else None


def kind_label(code: PromoCode) -> str:
    return texts.PC_KIND_FOREVER if code.kind == "forever" else texts.PC_KIND_DAYS.format(days=code.days or DAYS_DEFAULT)


def export_txt(codes: list[PromoCode]) -> str:
    """Тот же формат, что promo_codes.txt: разделы «Навсегда (N)» и «N дней (M)», по коду в строке.
    Отозванные коды не выгружаем."""
    live = [c for c in codes if c.revoked_at is None]
    forever = [c.code for c in live if c.kind == "forever"]
    by_days: dict[int, list[str]] = {}
    for c in live:
        if c.kind == "days":
            by_days.setdefault(c.days or DAYS_DEFAULT, []).append(c.code)
    parts = [texts.PC_TXT_FOREVER.format(n=len(forever)) + "\n" + "\n".join(forever)]
    for d in sorted(by_days):
        parts.append(texts.PC_TXT_DAYS.format(days=d, n=len(by_days[d])) + "\n" + "\n".join(by_days[d]))
    return "\n\n".join(parts) + "\n"


def parse_txt(text: str) -> dict[str, list[str]]:
    """Разделы promo_codes.txt → {заголовок: [коды]} (строки до первого раздела - пояснение, пропускаем)."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"(Навсегда|\d+ дней) \(\d+\)", line):
            current = line
            out[current] = []
        elif current is not None and normalize(line) == line:
            out[current].append(line)
    return out


# ------------------------------------------------------------------ планировщик
async def due_reminders(session: AsyncSession, now: datetime) -> list[User]:
    """30-дневный безлимит закончится в ближайшие 3 дня - напомнить один раз."""
    return list((await session.execute(select(User).where(
        User.unlimited_forever.is_(False), User.unlimited_until.is_not(None), User.unlimited_until > now,
        User.unlimited_until <= now + REMIND_BEFORE, User.unlimited_reminded_at.is_(None)))).scalars().all())


async def expire(session: AsyncSession, now: datetime) -> list[tuple[int, datetime]]:
    """Закончившиеся безлимиты снимаем. Заказы, отправленные во время безлимита, остаются бесплатными.
    Возвращает [(user_id, until)] - кому сказать «Безлимит закончился»."""
    async with app_lock():
        rows = (await session.execute(select(User).where(
            User.unlimited_forever.is_(False), User.unlimited_until.is_not(None), User.unlimited_until <= now)
            .execution_options(populate_existing=True))).scalars().all()
        out = []
        for user in rows:
            out.append((user.id, user.unlimited_until))
            _clear(user)
        if out:
            await session.commit()
        return out


async def unlimited_users(session: AsyncSession, now: datetime) -> list[User]:
    return list((await session.execute(select(User).where(
        (User.unlimited_forever.is_(True)) | (User.unlimited_until > now)).order_by(User.unlimited_since.desc())))
        .scalars().all())
