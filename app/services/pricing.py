"""Расчёт цены и разбиение оплаты. Чистые функции - их покрывают тесты."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal


def urgency_pct_for(hours: float, early: int, soon: int, urgent: int, free_hours: int = 48,
                    mid_hours: int = 24) -> int:
    """Множитель срочности в процентах (100 = ×1,0) по времени до дедлайна.

    От free_hours часов - early (обычно без наценки), от mid_hours до free_hours - soon, меньше mid_hours - urgent.
    """
    if hours >= free_hours:
        return early
    if hours >= mid_hours:
        return soon
    return urgent


def urgency_from_settings(s, hours: float) -> int:  # noqa: ANN001
    """То же по настройкам из админки (s - RuntimeSettings или dict)."""
    return urgency_pct_for(hours, int(s["urgency_ge72_pct"]), int(s["urgency_24_72_pct"]), int(s["urgency_lt24_pct"]),
                           int(s["urgency_free_hours"]), int(s["urgency_mid_hours"]))


def urgency_for_subject(s, subject: str | None, hours: float) -> int:  # noqa: ANN001
    """v2.4: множитель срочности с учётом предмета. Начерталка и расчётка: всегда 100 (×1,0), срок только
    для информации. Объяснение материала: по таблице срочности, как раньше."""
    if not s.urgency_for(subject):
        return 100
    return urgency_from_settings(s, hours)


def hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600


def calc_price(price_per_task: int, task_count: int, urgency_pct: int, discount_pct: int,
               free_tasks: int = 0) -> int:
    """цена за задачу × задачи × срочность × (1 − скидки), округление до рубля. Результат - копейки."""
    billable = max(int(task_count) - int(free_tasks), 0)
    discount_pct = min(max(int(discount_pct), 0), 100)
    raw = (Decimal(int(price_per_task)) * billable * Decimal(int(urgency_pct)) / 100
           * Decimal(100 - discount_pct) / 100)
    rubles = (raw / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(rubles) * 100


@dataclass(frozen=True)
class PaymentSplit:
    bonus: int
    balance: int
    invoice: int


def split_payment(price: int, bonus_available: int, balance_available: int, max_bonus_pct: int) -> PaymentSplit:
    """Сначала бонусы (не больше max_bonus_pct% цены), потом рублёвый баланс, остаток - счётом."""
    price = max(int(price), 0)
    cap = price * int(max_bonus_pct) // 100
    bonus = max(min(int(bonus_available), cap), 0)
    balance = max(min(int(balance_available), price - bonus), 0)
    return PaymentSplit(bonus=bonus, balance=balance, invoice=price - bonus - balance)
