"""Карточки заказов и строки цены."""
from __future__ import annotations

import html

from app import texts
from app.db.models import Order, User
from app.services import design as dsvc
from app.services.orders import ANY, EXPLAIN
from app.services.users import display_name
from app.utils.money import fmt_mult, fmt_rub, plural
from app.utils.timeutil import fmt_dt, fmt_left


def subject_name(code: str | None) -> str:
    if not code:
        return "-"
    return f"{texts.SUBJECT_ICONS.get(code, '')} {texts.SUBJECTS.get(code, code)}"


def tasks_word(n: int) -> str:
    return f"{n} {plural(n, ('задача', 'задачи', 'задач'))}"


def status_label(order: Order) -> str:
    """«📦 Готово»; у «🎨 Файлов и дизайна» принятый заказ - «✅ Принято», а не «🎓 Зачтено»."""
    if dsvc.is_design(order) and order.status in texts.DESIGN_STATUS:
        return texts.DESIGN_STATUS[order.status]
    return texts.STATUS.get(order.status, order.status)


def price_formula(order: Order) -> str:
    """«200 ₽ × 1 задача × срочность ×1,0 × (1 − 50%) = 100 ₽». «📎 Любая задача»: цену назвал админ.
    «🎨 Файлы и дизайн»: «🖼 Картинка × 3: <b>60 ₽</b>»."""
    if dsvc.is_design(order):
        return dsvc.price_line(order.design_kind or "", order.task_count or 1, order.price_auto)
    if order.subject == ANY:
        return texts.ANY_PRICE_WAIT if order.price_final is None else texts.ANY_PRICE_SET.format(
            price=fmt_rub(order.price_final))
    if order.price_per_task is None or order.urgency_pct is None:
        return "нет"
    parts = [fmt_rub(order.price_per_task)]
    if order.subject != EXPLAIN:  # у объяснения одна позиция - «× 1 задача» только путает
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
    lec = f"лекция: {texts.NOTE_STATUS[order.lecture_status]}" + (f" ({n_lecture} фото)" if n_lecture else "")
    pr = f"практика: {texts.NOTE_STATUS[order.practice_status]}" + (f" ({n_practice} фото)" if n_practice else "")
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
    icon = status_label(order).split(" ")[0]
    if dsvc.is_design(order):  # «✅ #5 · 🎨 📊 Презентация × 2, 🎓 Шаблон Самарского университета, 16:9, русский»
        star = (" ⭐" if priority else "") + (" ♾" if order.unlimited else "")
        return f"{icon} #{order.id} · 🎨 {dsvc.describe(order)}{star}"
    dl = fmt_dt(order.deadline_at).split(" ")[0] if order.deadline_at else "-"
    star = (" ⭐" if priority else "") + (" ♾" if order.unlimited else "")
    subject = texts.SUBJECT_ICONS.get(order.subject or "", "")
    what = (f"{subject} объяснение" if order.subject == EXPLAIN else f"{subject} задача" if order.subject == ANY
            else f"{subject}×{order.task_count or '?'}")
    if order.subject == ANY and not order.deadline_at:
        return f"{icon} #{order.id} · {what}{star}"
    return f"{icon} #{order.id} · {what} · до {dl}{star}"


def design_card(order: Order, counts: dict[str, int]) -> list[str]:
    """v2.4: что заказано в «🎨 Файлах и дизайне»: тип, оформление, количество, задание и цена."""
    lines = [f"Что: {dsvc.KINDS.get(order.design_kind or '', order.design_kind or '-')}"]
    style = dsvc.order_style(order)
    if style:
        lines.append(f"Оформление: {style}")
    lines.append(f"Количество: {order.task_count or 1}")
    lines.append(f"Файлов задания: {counts.get('condition', 0)}")
    if order.comment:
        lines.append(f"Задание: <i>{html.escape(order.comment)}</i>")
    if order.unlimited:
        lines.append("\n" + texts.ORDER_UNLIMITED_LINE)
    else:
        lines.append("\n" + price_formula(order))
    return lines


def order_card(order: Order, counts: dict[str, int], user: User | None = None, admin: bool = False,
               assignee: User | None = None) -> str:
    explain, anytask, design = order.subject == EXPLAIN, order.subject == ANY, dsvc.is_design(order)
    title = ("📸 <b>Объяснение #{id}</b>" if explain else "📎 <b>Задача #{id}</b>" if anytask
             else texts.DESIGN_CARD_TITLE if design else "📦 <b>Заказ #{id}</b>")
    lines = [f"{title.format(id=order.id)} · {status_label(order)}"]
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
    if design:
        lines += design_card(order, counts)
    elif explain or anytask:
        lines.append(f"Что: {subject_name(order.subject)}")
    else:
        lines.append(f"Предмет: {subject_name(order.subject)} · {tasks_word(order.task_count or 0)}"
                     + (f" · вариант {html.escape(order.variant)}" if order.variant else ""))
    if order.deadline_at and not design:
        lines.append(f"Дедлайн: {fmt_dt(order.deadline_at, with_weekday=True)} ({fmt_left(order.deadline_at)})")
    if not design:
        lines.append(f"{'Фото материала' if explain else 'Файлов условия' if anytask else 'Фото условия'}: "
                     f"{counts.get('condition', 0)}")
    nl = None if explain or anytask or design else notes_line(order, counts.get("lecture", 0),
                                                               counts.get("practice", 0))
    if nl:
        lines.append(nl)
    if order.comment and not design:
        label = "Вопрос" if explain else "Условие текстом" if anytask else "Комментарий"
        lines.append(f"{label}: <i>{html.escape(order.comment)}</i>")
    if design:
        pass
    elif order.status not in ("draft",) and order.unlimited:
        lines.append("\n" + texts.ORDER_UNLIMITED_LINE)
    elif order.status not in ("draft",):
        lines.append(f"\nЦена: {price_formula(order)}")
        if anytask and order.price_final is None:
            pass
        elif order.price_final is not None:
            lines.append(f"Итого к оплате: <b>{fmt_rub(order.price_final)}</b>")
        elif order.status == "review":
            lines.append(f"Итого сейчас: <b>{fmt_rub(order.price or 0)}</b> (счёт после проверки)")
    pl = paid_line(order)
    if pl:
        lines.append(pl)
    if order.reject_reason and order.status == "rejected":
        lines.append(f"Причина: {html.escape(order.reject_reason)}")
    if admin:
        lines.append(f"\nСоздан {fmt_dt(order.created_at)} · отправлен {fmt_dt(order.submitted_at)}"
                     + (f" · {'правок' if design else 'переделок'} {order.redo_count}" if order.redo_count else "")
                     + (" · был спор" if order.had_dispute else ""))
        if order.assignee_id:
            lines.append(f"🛠 Исполнитель: {display_name(assignee) if assignee else order.assignee_id}")
        if order.chat_replies:
            lines.append(f"✉️ Сообщений клиенту по заказу: {order.chat_replies}")
        if order.solution_entry:
            lines.append(f"{'Файл объяснения' if explain else 'Решение'}: <code>{html.escape(order.solution_entry)}</code>")
    return "\n".join(lines)
