"""Карточки заказов и строки цены."""
from __future__ import annotations

import html

from app import texts
from app.db.models import Order, User
from app.services.orders import EXPLAIN
from app.services.users import display_name
from app.utils.money import fmt_mult, fmt_rub, plural
from app.utils.timeutil import fmt_dt, fmt_left


def subject_name(code: str | None) -> str:
    if not code:
        return "—"
    return f"{texts.SUBJECT_ICONS.get(code, '')} {texts.SUBJECTS.get(code, code)}"


def tasks_word(n: int) -> str:
    return f"{n} {plural(n, ('задача', 'задачи', 'задач'))}"


def price_formula(order: Order) -> str:
    """«200 ₽ × 1 задача × срочность ×1,0 × (1 − 50%) = 100 ₽»."""
    if order.price_per_task is None or order.urgency_pct is None:
        return "—"
    parts = [fmt_rub(order.price_per_task)]
    if order.subject != EXPLAIN:  # у объяснения одна позиция — «× 1 задача» только путает
        parts.append(tasks_word(order.task_count or 0))
    parts.append(f"срочность {fmt_mult(order.urgency_pct)}")
    if order.discount_pct:
        parts.append(f"(1 − {order.discount_pct}%)")
    line = " × ".join(parts) + f" = {fmt_rub(order.price_auto or 0)}"
    if order.free_tasks:
        line += f"\n🎁 Бесплатных задач по «Месяцу бесплатно»: {order.free_tasks}"
    if order.price_override is not None:
        line += f"\n✏️ Цена поправлена вручную: {fmt_rub(order.price_override)}"
    return line


def notes_line(order: Order, n_lecture: int, n_practice: int) -> str | None:
    if not (order.lecture_discount_pct or order.practice_discount_pct or n_lecture or n_practice):
        return None
    lec = f"лекция — {texts.NOTE_STATUS[order.lecture_status]}" + (f" ({n_lecture} фото)" if n_lecture else "")
    pr = f"практика — {texts.NOTE_STATUS[order.practice_status]}" + (f" ({n_practice} фото)" if n_practice else "")
    return f"Конспекты: {lec}, {pr}"


def paid_line(order: Order) -> str | None:
    if not order.paid_at:
        return None
    parts = []
    if order.paid_bonus:
        parts.append(f"бонусами {fmt_rub(order.paid_bonus)}")
    if order.paid_balance:
        parts.append(f"с баланса {fmt_rub(order.paid_balance)}")
    if order.paid_invoice:
        parts.append(f"счётом {fmt_rub(order.paid_invoice)}")
    test = " 🧪 тест" if order.is_test else ""
    if order.unlimited and not parts:
        return texts.PAID_BY_PROMO.format(date=fmt_dt(order.paid_at)) + test
    return f"Оплачено {fmt_dt(order.paid_at)}: " + (", ".join(parts) or "бесплатно") + test


def unlimited_status(user: User | None, now=None) -> str | None:  # noqa: ANN001
    """«♾ Безлимит навсегда» / «♾ Безлимит до ДД.ММ.ГГГГ» или None."""
    from app.utils.timeutil import full_date, now_utc
    if user is None:
        return None
    if user.unlimited_forever:
        return texts.UNLIMITED_STATUS_FOREVER
    if user.unlimited_until and user.unlimited_until > (now or now_utc()):
        return texts.UNLIMITED_STATUS_UNTIL.format(date=full_date(user.unlimited_until))
    return None


def order_line(order: Order, priority: bool = False) -> str:
    icon = texts.STATUS.get(order.status, order.status).split(" ")[0]
    dl = fmt_dt(order.deadline_at).split(" ")[0] if order.deadline_at else "—"
    star = (" ⭐" if priority else "") + (" ♾" if order.unlimited else "")
    subject = texts.SUBJECT_ICONS.get(order.subject or "", "")
    what = f"{subject} объяснение" if order.subject == EXPLAIN else f"{subject}×{order.task_count or '?'}"
    return f"{icon} #{order.id} · {what} · до {dl}{star}"


def order_card(order: Order, counts: dict[str, int], user: User | None = None, admin: bool = False,
               assignee: User | None = None) -> str:
    explain = order.subject == EXPLAIN
    title = "📸 <b>Объяснение #{id}</b>" if explain else "📦 <b>Заказ #{id}</b>"
    lines = [f"{title.format(id=order.id)} — {texts.STATUS.get(order.status, order.status)}"]
    if admin and user is not None:
        status = []
        if user.is_priority:
            status.append("⭐ Приоритет")
        if user.free_month_until and user.free_month_tasks_left:
            status.append(f"🎁 Месяц бесплатно ({user.free_month_tasks_left} задач)")
        unl = unlimited_status(user)
        if unl:
            status.append(unl)
        lines.append(f"Клиент: {display_name(user)}" + (f" · {' · '.join(status)}" if status else ""))
    if explain:
        lines.append(f"Что: {subject_name(order.subject)}")
    else:
        lines.append(f"Предмет: {subject_name(order.subject)} · {tasks_word(order.task_count or 0)}"
                     + (f" · вариант {html.escape(order.variant)}" if order.variant else ""))
    if order.deadline_at:
        lines.append(f"Дедлайн: {fmt_dt(order.deadline_at, with_weekday=True)} ({fmt_left(order.deadline_at)})")
    lines.append(f"{'Фото материала' if explain else 'Фото условия'}: {counts.get('condition', 0)}")
    nl = None if explain else notes_line(order, counts.get("lecture", 0), counts.get("practice", 0))
    if nl:
        lines.append(nl)
    if order.comment:
        lines.append(f"{'Вопрос' if explain else 'Комментарий'}: <i>{html.escape(order.comment)}</i>")
    if order.status not in ("draft",) and order.unlimited:
        lines.append("\n" + texts.ORDER_UNLIMITED_LINE)
    elif order.status not in ("draft",):
        lines.append(f"\nЦена: {price_formula(order)}")
        if order.price_final is not None:
            lines.append(f"Итого к оплате: <b>{fmt_rub(order.price_final)}</b>")
        elif order.status == "review":
            lines.append(f"Итого сейчас: <b>{fmt_rub(order.price or 0)}</b> (счёт — после проверки)")
    pl = paid_line(order)
    if pl:
        lines.append(pl)
    if order.reject_reason and order.status == "rejected":
        lines.append(f"Причина: {html.escape(order.reject_reason)}")
    if admin:
        lines.append(f"\nСоздан {fmt_dt(order.created_at)} · отправлен {fmt_dt(order.submitted_at)}"
                     + (f" · переделок {order.redo_count}" if order.redo_count else "")
                     + (" · был спор" if order.had_dispute else ""))
        if order.assignee_id:
            lines.append(f"🛠 Исполнитель: {display_name(assignee) if assignee else order.assignee_id}")
        if order.chat_replies:
            lines.append(f"✉️ Сообщений клиенту по заказу: {order.chat_replies}")
        if order.solution_entry:
            lines.append(f"{'Файл объяснения' if explain else 'Решение'}: <code>{html.escape(order.solution_entry)}</code>")
    return "\n".join(lines)
