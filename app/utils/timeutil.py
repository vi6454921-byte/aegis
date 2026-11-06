"""Время: в БД - UTC, людям - Europe/Samara."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Samara")
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def local_today(now: datetime | None = None) -> date:
    return to_local(now or now_utc()).date()


def end_of_local_day(d: date) -> datetime:
    """23:59 указанного дня по Самаре (aware)."""
    return datetime.combine(d, time(23, 59), tzinfo=TZ)


def local_day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0, 0), tzinfo=TZ)
    return start, start + timedelta(days=1)


def fmt_dt(dt: datetime | None, with_weekday: bool = False) -> str:
    if dt is None:
        return "-"
    loc = to_local(dt)
    s = loc.strftime("%d.%m")
    if loc.year != local_today().year:
        s += loc.strftime(".%Y")
    s += loc.strftime(" %H:%M")
    if with_weekday:
        s = f"{WEEKDAYS[loc.weekday()]} {s}"
    return s


def full_date(dt: datetime | date | None) -> str:
    """ДД.ММ.ГГГГ (по Самаре) - для сроков безлимита."""
    if dt is None:
        return "-"
    d = to_local(dt).date() if isinstance(dt, datetime) else dt
    return d.strftime("%d.%m.%Y")


def fmt_date(dt: datetime | date | None) -> str:
    if dt is None:
        return "-"
    d = to_local(dt).date() if isinstance(dt, datetime) else dt
    s = d.strftime("%d.%m")
    if d.year != local_today().year:
        s += d.strftime(".%Y")
    return s


def fmt_left(target: datetime, now: datetime | None = None) -> str:
    delta = target - (now or now_utc())
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "срок прошёл"
    days, rem = divmod(secs, 86400)
    hours = rem // 3600
    if days:
        return f"через {days} дн. {hours} ч" if hours else f"через {days} дн."
    if hours:
        return f"через {hours} ч"
    return f"через {max(rem // 60, 1)} мин"


_DATE_RE = re.compile(
    r"^\s*(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?(?:[\s,]+(\d{1,2})[:.](\d{2}))?\s*$"
)
_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[\sT]+(\d{1,2}):(\d{2}))?\s*$")


def parse_deadline(text: str, now: datetime | None = None) -> datetime | None:
    """«25.03», «25.03.2026», «25.03 18:00», «2026-03-25 18:00» → aware datetime.

    Без времени - 23:59 этого дня. Без года - ближайшая будущая такая дата.
    """
    now = now or now_utc()
    today = local_today(now)
    year_given = True
    m = _DATE_RE.match(text or "")
    if m:
        day, month, year, hh, mm = m.groups()
        if year:
            y = int(year)
            if y < 100:
                y += 2000
        else:
            y, year_given = today.year, False
    else:
        m = _ISO_RE.match(text or "")
        if not m:
            return None
        year, month, day, hh, mm = m.groups()
        y = int(year)
    try:
        d = date(y, int(month), int(day))
        if not year_given and d < today:
            d = date(y + 1, int(month), int(day))
    except ValueError:
        return None
    if hh is not None:
        h, mi = int(hh), int(mm)
        if h > 23 or mi > 59:
            return None
        return datetime.combine(d, time(h, mi), tzinfo=TZ)
    return end_of_local_day(d)
