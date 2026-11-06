"""Деньги - целые копейки. Здесь форматирование и разбор ввода."""
from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NBSP = "\u00a0"
# Деньги хранятся в BIGINT (копейки): больше этого числа не влезает ни в один баланс и ни в одну операцию.
INT64_MAX = 9_223_372_036_854_775_807


def fmt_rub(kop: int) -> str:
    sign = "−" if kop < 0 else ""
    kop = abs(int(kop))
    rub, k = divmod(kop, 100)
    rub_s = f"{rub:,}".replace(",", NBSP)
    if k:
        return f"{sign}{rub_s},{k:02d}{NBSP}₽"
    return f"{sign}{rub_s}{NBSP}₽"


def parse_rub(text: str) -> int | None:
    """«50», «50,5», «1 000», «99.90 ₽» → копейки (>0) или None."""
    if text is None:
        return None
    t = re.sub(r"[\s\u00a0₽руб.]+$", "", text.strip().lower())
    t = t.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", t):
        return None
    value = (Decimal(t) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(value) if value > 0 else None


_SIGNED_RE = re.compile(r"([+\-−\u2013]?)(\d+(?:[.,]\d{1,2})?)")


def parse_signed_rub(text: str) -> int | None:
    """«500», «+500», «-500», «−1 000,50», «99.90 ₽» → копейки со знаком (без верхнего предела) или None.

    Ноль и мусор - None. Переполнение хранения проверяет вызывающий (fits_int64)."""
    if text is None:
        return None
    t = re.sub(r"[\s\u00a0]+", "", text.strip().lower())
    t = re.sub(r"(₽|руб\.?|р\.?)$", "", t)
    m = _SIGNED_RE.fullmatch(t)
    if not m:
        return None
    value = int((Decimal(m.group(2).replace(",", ".")) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if value == 0:
        return None
    return -value if m.group(1) in ("-", "−", "\u2013") else value


_AMOUNT_HEAD_RE = re.compile(r"^\s*([+\-−\u2013]?)\s*(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)([.,]\d{1,2})?"
                             r"\s*(?:₽|руб\.?|р\.?)?(?=\s|$)\s*(.*)$", re.IGNORECASE | re.DOTALL)


def split_amount(text: str | None) -> tuple[int | None, str]:
    """Сумма в начале строки и всё, что после неё: «+1 000,50 за пост» → (100050, "за пост"), «-500» → (-50000, "").

    Разряды можно отделять пробелом («10 000»). Не сумма (или ноль) - (None, "")."""
    m = _AMOUNT_HEAD_RE.match(text or "")
    if not m:
        return None, ""
    sign, digits, frac, rest = m.groups()
    digits = digits.replace(" ", "").replace("\u00a0", "")
    return parse_signed_rub(f"{sign}{digits}{frac or ''}"), rest.strip()


def fits_int64(*values: int) -> bool:
    """Каждое значение помещается в 64-битное целое со знаком (так деньги лежат в БД)."""
    return all(-INT64_MAX - 1 <= int(v) <= INT64_MAX for v in values)


def fmt_mult(pct: int) -> str:
    v = Decimal(pct) / 100
    s = f"{v:.2f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return "×" + s.replace(".", ",")


def parse_mult(text: str) -> int | None:
    """«1,5» / «×2» / «x1.25» → проценты (150 / 200 / 125)."""
    t = (text or "").strip().lower().lstrip("×x*").replace(",", ".").strip()
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    if v <= 0 or v > 10:
        return None
    return int((v * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def stars_for(kop: int, rub_per_star: Decimal | str) -> int:
    """Сумма в звёздах = ceil(рубли / RUB_PER_STAR)."""
    if kop <= 0:
        return 0
    rate = Decimal(str(rub_per_star))
    return max(1, math.ceil(Decimal(kop) / (rate * 100)))


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]
