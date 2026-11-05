from decimal import Decimal

import pytest

from app.services.pricing import calc_price, split_payment, urgency_pct_for
from app.utils.money import fmt_rub, parse_rub, stars_for

U = (100, 150, 200)


@pytest.mark.parametrize(("hours", "pct"), [(200, 100), (72, 100), (48, 100), (47.99, 150), (24, 150),
                                            (23.99, 200), (1, 200), (0, 200)])
def test_urgency_boundaries(hours: float, pct: int) -> None:
    """По умолчанию: от 48 ч — ×1,0, от 24 до 48 ч — ×1,5, меньше 24 ч — ×2,0."""
    assert urgency_pct_for(hours, *U) == pct


@pytest.mark.parametrize(("hours", "pct"), [(72, 100), (71.99, 150), (12, 150), (11.99, 200)])
def test_urgency_custom_thresholds(hours: float, pct: int) -> None:
    """Пороги меняются в настройках: например, без наценки от 72 ч, максимальная — меньше 12 ч."""
    assert urgency_pct_for(hours, *U, free_hours=72, mid_hours=12) == pct


def test_formula_and_discounts() -> None:
    # 200 ₽ × 1 задача × 1,0 × (1 − 25% − 25%) = 100 ₽ — шаг 3 сценария
    assert calc_price(20000, 1, 100, 50) == 10000
    assert calc_price(20000, 1, 100, 25) == 15000
    assert calc_price(20000, 3, 150, 0) == 90000
    assert calc_price(20000, 2, 200, 25) == 60000
    assert calc_price(20000, 1, 150, 50) == 15000


def test_rounding_to_ruble_half_up() -> None:
    assert calc_price(19950, 1, 100, 0) == 20000  # 199,50 → 200
    assert calc_price(19949, 1, 100, 0) == 19900  # 199,49 → 199
    assert calc_price(33333, 1, 150, 0) == 50000  # 499,995 → 500
    assert calc_price(10001, 1, 100, 25) == 7500  # 75,0075 → 75
    assert calc_price(1, 1, 100, 0) == 0


def test_free_tasks_and_bounds() -> None:
    assert calc_price(20000, 3, 100, 0, free_tasks=1) == 40000
    assert calc_price(20000, 3, 100, 0, free_tasks=5) == 0
    assert calc_price(20000, 1, 100, 150) == 0  # скидка не больше 100%
    assert calc_price(20000, 1, 100, -10) == 20000


def test_split_payment_bonus_cap_50_percent() -> None:
    sp = split_payment(10000, bonus_available=8000, balance_available=3000, max_bonus_pct=50)
    assert (sp.bonus, sp.balance, sp.invoice) == (5000, 3000, 2000)
    sp = split_payment(10000, bonus_available=2000, balance_available=0, max_bonus_pct=50)
    assert (sp.bonus, sp.balance, sp.invoice) == (2000, 0, 8000)
    sp = split_payment(10000, bonus_available=9000, balance_available=20000, max_bonus_pct=50)
    assert (sp.bonus, sp.balance, sp.invoice) == (5000, 5000, 0)
    sp = split_payment(10001, bonus_available=9000, balance_available=0, max_bonus_pct=50)
    assert sp.bonus == 5000 and sp.invoice == 5001  # бонусами — не больше половины, округляем вниз


def test_stars_and_money_format() -> None:
    assert stars_for(10000, Decimal("1.75")) == 58  # ceil(100 / 1,75)
    assert stars_for(17500, "1.75") == 100
    assert stars_for(1, "1.75") == 1
    assert stars_for(0, "1.75") == 0
    assert fmt_rub(10000) == "100\u00a0₽"
    assert fmt_rub(-5050) == "−50,50\u00a0₽"
    assert parse_rub("1 000,5") == 100050
    assert parse_rub("abc") is None
    assert parse_rub("0") is None
