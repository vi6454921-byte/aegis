"""Жизненный цикл заказа: черновик → на проверке → ждёт оплаты → оплачен → в работе → готово → зачтено/спор/возврат.

v2.4, «📎 Любая задача»: черновик → ждёт оценки → оценено (цену назвал админ) → оплачен → дальше как обычно.
"""
from __future__ import annotations

import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.db.models import Order, OrderFile, Payment, User
from app.services import slots
from app.services.admin_log import log_admin
from app.services.ledger import lock_user
from app.services.locks import app_lock
from app.services.pricing import calc_price, hours_between, urgency_for_subject
from app.services.referrals import RewardResult, reward_for_delivered
from app.utils.timeutil import local_today, to_local

# v2.4: «📎 Любая задача»: estimate (ждёт оценки) → админ называет цену → quoted (оценено) → оплата → paid
ESTIMATE, QUOTED = "estimate", "quoted"
ACTIVE = (ESTIMATE, QUOTED, "review", "awaiting_payment", "paid", "in_work", "dispute")
QUEUE_DEFAULT = ACTIVE
PAID_LIKE = ("paid", "in_work", "done", "accepted", "dispute")
REFUNDABLE = ("paid", "in_work", "done", "dispute")
CANCELLABLE = (ESTIMATE, QUOTED, "review", "awaiting_payment")
PAYABLE = ("awaiting_payment", QUOTED)  # можно оплатить: счёт выставлен или цена названа
MAX_FILES = 30  # фото условия: альбомы принимаем целиком (в Telegram альбом - до 10 штук)
MAX_TASKS = 30
EXPLAIN = "explain"  # «Объяснение материала»: одна позиция, без варианта и конспектов
EXPLAIN_ALL = "Объясните всю тему целиком"
ANY = "any"  # v2.4: «📎 Любая задача»: условие любого предмета, цену называет админ
MAX_ANY_TEXT = 3000  # текст условия любой задачи (несколько сообщений подряд склеиваем)
QUOTE_MIN, QUOTE_MAX = 100, 100_000 * 100  # цена оценки: от 1 ₽ до 100 000 ₽ (копейки)


def is_explain(order: Order | None) -> bool:
    return order is not None and order.subject == EXPLAIN


def is_any(order: Order | None) -> bool:
    return order is not None and order.subject == ANY


class OrderError(Exception):
    """Ошибка с текстом для человека."""


class IntakeClosed(OrderError):
    def __init__(self, state: slots.IntakeState):
        super().__init__("intake closed")
        self.state = state


class NotEnoughSlots(OrderError):
    def __init__(self, remaining: int):
        super().__init__("not enough slots")
        self.remaining = remaining


async def get_order(session: AsyncSession, order_id: int) -> Order | None:
    return await session.get(Order, order_id, populate_existing=True)


async def _require(session: AsyncSession, order_id: int, statuses: tuple[str, ...] | None = None,
                   user_id: int | None = None) -> Order:
    order = await get_order(session, order_id)
    if order is None or (user_id is not None and order.user_id != user_id):
        raise OrderError("Заказ не найден.")
    if statuses is not None and order.status not in statuses:
        raise OrderError("Статус заказа уже изменился: открой его заново.")
    return order


# ------------------------------------------------------------------ черновик
async def get_draft(session: AsyncSession, user_id: int) -> Order | None:
    return await session.scalar(select(Order).where(Order.user_id == user_id, Order.status == "draft")
                                .order_by(Order.id.desc()).limit(1)
                                .execution_options(populate_existing=True))


async def create_draft(session: AsyncSession, user_id: int, subject: str | None, now: datetime) -> Order:
    """subject=None - фото прислали раньше, чем выбрали предмет: черновик всё равно сохраняем."""
    order = Order(user_id=user_id, subject=subject, status="draft", wizard_step="photos",
                  task_count=1 if subject == EXPLAIN else None, created_at=now, updated_at=now)
    session.add(order)
    await session.commit()
    return order


async def files_of(session: AsyncSession, order_id: int, kind: str | None = None) -> list[OrderFile]:
    q = select(OrderFile).where(OrderFile.order_id == order_id)
    if kind:
        q = q.where(OrderFile.kind == kind)
    return list((await session.execute(q.order_by(OrderFile.id))).scalars().all())


async def count_files(session: AsyncSession, order_id: int, kind: str) -> int:
    return int(await session.scalar(select(func.count(OrderFile.id)).where(
        OrderFile.order_id == order_id, OrderFile.kind == kind)) or 0)


async def add_file(session: AsyncSession, order_id: int, kind: str, **fields) -> OrderFile:
    f = OrderFile(order_id=order_id, kind=kind, **fields)
    session.add(f)
    await session.commit()
    return f


async def clear_files(session: AsyncSession, order_id: int, kind: str) -> None:
    for f in await files_of(session, order_id, kind):
        if f.local_path:
            Path(f.local_path).unlink(missing_ok=True)
    await session.execute(delete(OrderFile).where(OrderFile.order_id == order_id, OrderFile.kind == kind))
    await session.commit()


async def delete_draft(session: AsyncSession, order: Order, uploads_dir: Path) -> None:
    if order.status != "draft":
        return
    await session.execute(delete(OrderFile).where(OrderFile.order_id == order.id))
    await session.execute(delete(Order).where(Order.id == order.id))
    await session.commit()
    shutil.rmtree(uploads_dir / str(order.id), ignore_errors=True)


@dataclass
class DraftPrice:
    price_per_task: int
    urgency_pct: int
    base: int
    with_notes: int
    potential_discount: int


def draft_price(order: Order, s, now: datetime, has_lecture: bool, has_practice: bool) -> DraftPrice:  # noqa: ANN001
    ppt = s.price_for(order.subject)
    urg = urgency_for_subject(s, order.subject, hours_between(now, order.deadline_at)) if order.deadline_at else 100
    disc = 0
    if s.notes_discount_for(order.subject):
        disc = (s.discount_lecture_pct if has_lecture else 0) + (s.discount_practice_pct if has_practice else 0)
    count = order.task_count or 1
    return DraftPrice(ppt, urg, calc_price(ppt, count, urg, 0), calc_price(ppt, count, urg, disc), disc)


def recalc(order: Order) -> None:
    disc = (order.lecture_discount_pct if order.lecture_status == "approved" else 0) + \
           (order.practice_discount_pct if order.practice_status == "approved" else 0)
    order.discount_pct = min(disc, 100)
    order.price_auto = calc_price(order.price_per_task, order.task_count, order.urgency_pct, order.discount_pct)


async def submit_order(session: AsyncSession, order_id: int, s, now: datetime) -> Order:  # noqa: ANN001
    async with app_lock():
        order = await get_order(session, order_id)
        if order is None or order.status != "draft":
            raise OrderError("Этот черновик уже отправлен или удалён.")
        if not order.subject or not order.task_count or not order.deadline_at \
                or await count_files(session, order.id, "condition") == 0 \
                or (order.subject == EXPLAIN and not (order.comment or "").strip()):
            raise OrderError("Заказ заполнен не полностью: пройди шаги ещё раз.")
        if order.deadline_at <= now:
            raise OrderError("Дедлайн уже прошёл: выбери новый.")
        user = await session.get(User, order.user_id, populate_existing=True)
        unlimited = bool(user and user.unlimited_active(now))
        intake = await slots.intake_state(session, s, now, unlimited=unlimited)
        if not intake.open:
            raise IntakeClosed(intake)
        if intake.remaining is not None and order.task_count > intake.remaining:
            raise NotEnoughSlots(intake.remaining)
        order.price_per_task = s.price_for(order.subject)
        order.urgency_pct = urgency_for_subject(s, order.subject, hours_between(now, order.deadline_at))
        if s.notes_discount_for(order.subject):
            order.lecture_discount_pct = int(s.discount_lecture_pct)
            order.practice_discount_pct = int(s.discount_practice_pct)
            order.lecture_status = "pending" if await count_files(session, order.id, "lecture") else "none"
            order.practice_status = "pending" if await count_files(session, order.id, "practice") else "none"
        else:
            order.lecture_discount_pct = order.practice_discount_pct = 0
            order.lecture_status = order.practice_status = "none"
        recalc(order)
        order.unlimited = unlimited
        order.status = "review"
        order.submitted_at = now
        order.draft_warned_at = None
        order.slot_date = local_today(now)
        order.wizard_step = None
        await session.commit()
        return order


async def submit_any(session: AsyncSession, order_id: int, s, now: datetime) -> Order:  # noqa: ANN001
    """v2.4: «📎 Любая задача» → статус «ждёт оценки». Срок и цену не считаем: цену назовёт админ.
    Безлимит (промокод) и «Месяц бесплатно» на такие задачи не действуют: цена у каждой своя."""
    async with app_lock():
        order = await get_order(session, order_id)
        if order is None or order.status != "draft" or order.subject != ANY:
            raise OrderError("Этот черновик уже отправлен или удалён.")
        if await count_files(session, order.id, "condition") == 0 and not (order.comment or "").strip():
            raise OrderError(texts.ANY_NEED_CONTENT)
        intake = await slots.intake_state(session, s, now)
        if not intake.open:
            raise IntakeClosed(intake)
        if intake.remaining is not None and intake.remaining < 1:
            raise NotEnoughSlots(intake.remaining)
        order.task_count = 1
        order.price_per_task = order.urgency_pct = order.price_auto = order.price_final = None
        order.lecture_discount_pct = order.practice_discount_pct = order.discount_pct = 0
        order.lecture_status = order.practice_status = "none"
        order.unlimited = False
        order.status = ESTIMATE
        order.submitted_at = now
        order.draft_warned_at = None
        order.slot_date = local_today(now)
        order.wizard_step = None
        await session.commit()
        return order


async def quote(session: AsyncSession, order_id: int, price: int, now: datetime, admin_id: int) -> Order:
    """v2.4: админ называет цену любой задачи (рубли → копейки). Можно поменять, пока клиент не оплатил."""
    if not QUOTE_MIN <= int(price) <= QUOTE_MAX:
        raise OrderError(texts.QUOTE_RANGE)
    async with app_lock():
        order = await _require(session, order_id, (ESTIMATE, QUOTED))
        if order.subject != ANY:
            raise OrderError("Цену так назначают только для «📎 Любой задачи».")
        old = order.price_final
        await _release(session, order, now)  # если цену меняют: старый счёт больше не действует
        order.price_per_task = order.price_auto = order.price_final = int(price)
        order.task_count, order.urgency_pct, order.discount_pct, order.price_override = 1, 100, 0, None
        order.status = QUOTED
        order.quoted_at = order.invoiced_at = now
        order.quoted_by = admin_id
        log_admin(session, admin_id, "quote_price", "order", order.id, price=int(price), old=old)
        await session.commit()
        return order


# ------------------------------------------------------------------ админ: проверка и счёт
async def set_note_status(session: AsyncSession, order_id: int, which: str, approved: bool,
                          admin_id: int) -> Order:
    if which not in ("lecture", "practice"):
        raise OrderError("Неизвестный конспект.")
    order = await _require(session, order_id, ("review",))
    if getattr(order, f"{which}_status") == "none":
        raise OrderError("Этого конспекта нет в заказе.")
    setattr(order, f"{which}_status", "approved" if approved else "rejected")
    recalc(order)
    log_admin(session, admin_id, f"note_{'approve' if approved else 'reject'}", "order", order.id, note=which)
    await session.commit()
    return order


async def set_task_count(session: AsyncSession, order_id: int, count: int, admin_id: int) -> Order:
    if not 1 <= count <= MAX_TASKS:
        raise OrderError(f"Количество: от 1 до {MAX_TASKS}.")
    order = await _require(session, order_id, ("review",))
    old = order.task_count
    order.task_count = count
    recalc(order)
    log_admin(session, admin_id, "set_task_count", "order", order.id, old=old, new=count)
    await session.commit()
    return order


async def set_price_override(session: AsyncSession, order_id: int, price: int | None, admin_id: int) -> Order:
    order = await _require(session, order_id, ("review",))
    old = order.price_override
    order.price_override = price
    log_admin(session, admin_id, "set_price", "order", order.id, old=old, new=price)
    await session.commit()
    return order


async def issue_invoice(session: AsyncSession, order_id: int, s, now: datetime, admin_id: int) -> Order:  # noqa: ANN001
    async with app_lock():
        order = await _require(session, order_id, ("review",))
        user = await lock_user(session, order.user_id)
        free = 0
        if user.unlimited_active(now):
            order.unlimited = True
        # по безлимиту цена 0 ₽ - скидка за конспекты ни на что не влияет, проверять их не обязательно
        if not order.unlimited and "pending" in (order.lecture_status, order.practice_status):
            raise OrderError("Сначала проверь конспекты: одобри или отклони.")
        # «Месяц бесплатно» - это бесплатные задачи; объяснение материала задачей не считается.
        # Безлимит (промокод) - заказ бесплатный целиком, «Месяц бесплатно» не тратим.
        if order.unlimited:
            pass
        elif order.subject != EXPLAIN and user.free_month_until and user.free_month_until > now \
                and user.free_month_tasks_left > 0:
            free = min(order.task_count, user.free_month_tasks_left)
            user.free_month_tasks_left -= free
        order.free_tasks = free
        if order.unlimited:
            price = 0
        elif order.price_override is not None:
            price = order.price_override
        else:
            price = calc_price(order.price_per_task, order.task_count, order.urgency_pct, order.discount_pct, free)
        order.price_final = price
        order.invoiced_at = now
        if price == 0:
            order.status = "paid"
            order.paid_at = now
        else:
            order.status = "awaiting_payment"
        log_admin(session, admin_id, "issue_invoice", "order", order.id, price=price, free_tasks=free,
                  **({"method": "promo_unlimited"} if order.unlimited else {}))
        await session.commit()
        return order


async def _release(session: AsyncSession, order: Order, now: datetime) -> None:
    if order.free_tasks:
        user = await lock_user(session, order.user_id)
        if user.free_month_until and user.free_month_until > now:
            user.free_month_tasks_left += order.free_tasks
        order.free_tasks = 0
    await session.execute(update(Payment).where(Payment.order_id == order.id, Payment.status == "pending")
                          .values(status="cancelled"))


async def back_to_review(session: AsyncSession, order_id: int, now: datetime, admin_id: int) -> Order:
    async with app_lock():
        order = await _require(session, order_id, ("awaiting_payment",))
        await _release(session, order, now)
        order.status = "review"
        order.price_final = None
        order.invoiced_at = None
        log_admin(session, admin_id, "back_to_review", "order", order.id)
        await session.commit()
        return order


async def cancel_by_user(session: AsyncSession, order_id: int, user_id: int, now: datetime) -> Order:
    async with app_lock():
        order = await _require(session, order_id, CANCELLABLE, user_id=user_id)
        await _release(session, order, now)
        order.status = "cancelled"
        order.closed_at = now
        await session.commit()
        return order


async def reject(session: AsyncSession, order_id: int, reason: str | None, now: datetime, admin_id: int) -> Order:
    async with app_lock():
        order = await _require(session, order_id, CANCELLABLE)
        await _release(session, order, now)
        order.status = "rejected"
        order.reject_reason = reason
        order.closed_at = now
        log_admin(session, admin_id, "reject_order", "order", order.id, reason=reason)
        await session.commit()
        return order


# ------------------------------------------------------------------ работа
async def take_in_work(session: AsyncSession, order_id: int, now: datetime, admin_id: int) -> Order:
    order = await _require(session, order_id, ("paid",))
    order.status = "in_work"
    order.taken_at = now
    order.assignee_id = admin_id
    log_admin(session, admin_id, "take_in_work", "order", order.id)
    await session.commit()
    return order


async def attach_solution(session: AsyncSession, order_id: int, entry: str, files: list[dict],
                          now: datetime, admin_id: int) -> Order:
    order = await _require(session, order_id, ("paid", "in_work", "done"))
    if order.status == "paid":
        order.status = "in_work"
        order.taken_at = now
    if order.assignee_id is None:
        order.assignee_id = admin_id
    await session.execute(delete(OrderFile).where(OrderFile.order_id == order.id, OrderFile.kind == "solution"))
    for f in files:
        session.add(OrderFile(order_id=order.id, kind="solution", file_type="document", **f))
    order.solution_entry = entry
    if not order.solution_token:
        order.solution_token = secrets.token_urlsafe(24)
    log_admin(session, admin_id, "upload_solution", "order", order.id, entry=entry, files=len(files))
    await session.commit()
    return order


async def mark_done(session: AsyncSession, order_id: int, s, now: datetime,  # noqa: ANN001
                    admin_id: int) -> tuple[Order, RewardResult | None]:
    async with app_lock():
        order = await _require(session, order_id, ("in_work",))
        if not can_finish(order):
            raise OrderError(texts.EXPLAIN_NEED_ANSWER if order.subject == EXPLAIN else texts.SOLUTION_NEEDED)
        order.status = "done"
        order.done_at = now
        reward = await reward_for_delivered(session, order, s, now)
        log_admin(session, admin_id, "mark_done", "order", order.id,
                  referral_reward=reward.amount if reward else None)
        await session.commit()
        return order, reward


def can_finish(order: Order) -> bool:
    """Решение есть - можно выдавать. Объяснение можно выдать и сообщениями в чате (без файла)."""
    return bool(order.solution_entry) or (order.subject == EXPLAIN and order.chat_replies > 0)


async def note_chat_reply(session: AsyncSession, order_id: int, admin_id: int, delivered: bool) -> Order | None:
    """Админ написал клиенту по заказу: считаем сообщения (для объяснений это и есть результат)."""
    order = await get_order(session, order_id)
    if order is None:
        return None
    if delivered:
        order.chat_replies += 1
        if order.status in ("paid", "in_work") and order.assignee_id is None:
            order.assignee_id = admin_id
    log_admin(session, admin_id, "message_client", "order", order.id, delivered=delivered)
    await session.commit()
    return order


async def accept(session: AsyncSession, order_id: int, user_id: int, now: datetime) -> Order:
    order = await _require(session, order_id, ("done",), user_id=user_id)
    order.status = "accepted"
    order.closed_at = now
    await session.commit()
    return order


async def open_dispute(session: AsyncSession, order_id: int, user_id: int, now: datetime) -> Order:
    order = await _require(session, order_id, ("done",), user_id=user_id)
    order.status = "dispute"
    order.had_dispute = True
    await session.commit()
    return order


async def redo(session: AsyncSession, order_id: int, now: datetime, admin_id: int) -> Order:
    order = await _require(session, order_id, ("dispute",))
    order.status = "in_work"
    order.redo_count += 1
    log_admin(session, admin_id, "redo", "order", order.id)
    await session.commit()
    return order


# ------------------------------------------------------------------ списки
def _queue_key(order: Order, user: User) -> tuple:
    d = to_local(order.deadline_at).date() if order.deadline_at else None
    return (d is None, d, not user.is_priority, order.deadline_at, order.id)


async def admin_list(session: AsyncSession, statuses: tuple[str, ...] | None, page: int,
                     per_page: int = 8) -> tuple[list[tuple[Order, User]], int]:
    q = select(Order, User).join(User, User.id == Order.user_id).where(Order.status != "draft")
    if statuses:
        q = q.where(Order.status.in_(statuses))
        rows = [(o, u) for o, u in (await session.execute(q)).all()]
        rows.sort(key=lambda r: _queue_key(*r))
        total = len(rows)
        return rows[page * per_page:(page + 1) * per_page], total
    total = int(await session.scalar(select(func.count(Order.id)).where(Order.status != "draft")) or 0)
    rows = (await session.execute(q.order_by(Order.id.desc()).offset(page * per_page).limit(per_page))).all()
    return [(o, u) for o, u in rows], total


async def status_counts(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(select(Order.status, func.count(Order.id)).where(Order.status != "draft")
                                  .group_by(Order.status))).all()
    return {status: int(n) for status, n in rows}


async def nearest_deadline(session: AsyncSession, now: datetime) -> Order | None:
    """Ближайший дедлайн среди оплаченных/в работе/спорных - для сводки админа."""
    return await session.scalar(select(Order).where(Order.status.in_(("paid", "in_work", "dispute")),
                                                    Order.deadline_at.is_not(None))
                                .order_by(Order.deadline_at).limit(1))


async def user_orders(session: AsyncSession, user_id: int, page: int, per_page: int = 8) -> tuple[list[Order], int]:
    base = select(Order).where(Order.user_id == user_id, Order.status != "draft")
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = (await session.execute(base.order_by(Order.id.desc()).offset(page * per_page).limit(per_page))).scalars()
    return list(rows), total
