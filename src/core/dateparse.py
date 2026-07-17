from __future__ import annotations

import re
from datetime import date


FULLWIDTH_TRANSLATION = str.maketrans(
    "０１２３４５６７８９／－．：％",
    "0123456789/-.:%",
)


def normalize_digits(text: str | None) -> str:
    return (text or "").translate(FULLWIDTH_TRANSLATION)


def clean_text(text: str | None) -> str:
    normalized = normalize_digits(text)
    return re.sub(r"\s+", " ", normalized).strip()


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_token(token: str, default_year: int | None = None) -> date | None:
    value = clean_text(token)
    value = re.sub(r"\([^)]+\)|（[^）]+）", "", value)

    m = re.search(r"(?P<y>20\d{2})[/-](?P<m>\d{1,2})[/-](?P<d>\d{1,2})", value)
    if m:
        return _safe_date(int(m.group("y")), int(m.group("m")), int(m.group("d")))

    m = re.search(r"(?P<y>20\d{2})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日", value)
    if m:
        return _safe_date(int(m.group("y")), int(m.group("m")), int(m.group("d")))

    m = re.search(r"(?P<m>\d{1,2})[/-](?P<d>\d{1,2})", value)
    if m and default_year:
        return _safe_date(default_year, int(m.group("m")), int(m.group("d")))

    m = re.search(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日", value)
    if m and default_year:
        return _safe_date(default_year, int(m.group("m")), int(m.group("d")))

    return None


DATE_PATTERN = re.compile(
    r"(?:20\d{2}[/-]\d{1,2}[/-]\d{1,2}|20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}[/-]\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日)"
)


def find_dates(text: str, default_year: int | None = None) -> list[date]:
    normalized = clean_text(text)
    dates: list[date] = []
    for match in DATE_PATTERN.finditer(normalized):
        parsed = parse_date_token(match.group(0), default_year=default_year)
        if parsed:
            dates.append(parsed)
    return dates


def first_date_near_keywords(
    text: str,
    keywords: list[str],
    default_year: int | None = None,
    window: int = 180,
    fallback_any: bool = True,
) -> tuple[date | None, str | None]:
    normalized = clean_text(text)
    for keyword in keywords:
        pos = normalized.find(keyword)
        if pos < 0:
            continue
        snippet = normalized[pos : pos + window]
        dates = find_dates(snippet, default_year=default_year)
        if dates:
            return dates[0], snippet
    if fallback_any:
        dates = find_dates(normalized, default_year=default_year)
        return (dates[0], None) if dates else (None, None)
    return None, None
