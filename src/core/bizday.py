from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import jpholiday
except ImportError:  # pragma: no cover - production installs requirements.txt
    jpholiday = None


JST = ZoneInfo("Asia/Tokyo")
YEAR_END_AND_NEW_YEAR = {(12, 31), (1, 1), (1, 2), (1, 3)}


def as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(JST).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def now_jst() -> datetime:
    return datetime.now(JST)


def today_jst() -> date:
    return now_jst().date()


def is_business_day(day: date | datetime | str) -> bool:
    target = as_date(day)
    if target.weekday() >= 5:
        return False
    if (target.month, target.day) in YEAR_END_AND_NEW_YEAR:
        return False
    if jpholiday is not None and jpholiday.is_holiday(target):
        return False
    if jpholiday is None and _is_fallback_holiday(target):
        return False
    return True


def add_business_days(day: date | datetime | str, n: int) -> date:
    target = as_date(day)
    if n == 0:
        return target

    step = 1 if n > 0 else -1
    remaining = abs(n)
    current = target
    while remaining:
        current += timedelta(days=step)
        if is_business_day(current):
            remaining -= 1
    return current


def next_business_day(day: date | datetime | str) -> date:
    return add_business_days(day, 1)


def prev_business_day(day: date | datetime | str) -> date:
    return add_business_days(day, -1)


def to_jst_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(JST) if value.tzinfo else value.replace(tzinfo=JST)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(JST) if parsed.tzinfo else parsed.replace(tzinfo=JST)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (nth - 1))


def _is_fallback_holiday(day: date) -> bool:
    fixed = {
        (1, 1),
        (2, 11),
        (2, 23),
        (4, 29),
        (5, 3),
        (5, 4),
        (5, 5),
        (8, 11),
        (11, 3),
        (11, 23),
    }
    if (day.month, day.day) in fixed:
        return True
    moving = {
        _nth_weekday(day.year, 1, 0, 2),
        _nth_weekday(day.year, 7, 0, 3),
        _nth_weekday(day.year, 9, 0, 3),
        _nth_weekday(day.year, 10, 0, 2),
    }
    return day in moving
