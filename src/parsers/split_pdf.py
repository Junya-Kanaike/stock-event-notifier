from __future__ import annotations

import re
from datetime import date
from typing import Any

from src.core.dateparse import clean_text, first_date_near_keywords


def extract_split_ratio(text: str) -> str | None:
    normalized = clean_text(text)
    patterns = [
        r"1\s*[:：対]\s*([0-9]+(?:\.[0-9]+)?)",
        r"1株につき\s*([0-9]+(?:\.[0-9]+)?)\s*株",
        r"普通株式1株を\s*([0-9]+(?:\.[0-9]+)?)\s*株",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return match.group(1).rstrip("0").rstrip(".")
    return None


def parse_split_details(text: str, disclosure_date: date) -> dict[str, Any]:
    effective_date, raw = first_date_near_keywords(
        text,
        ["効力発生日", "効力発生予定日"],
        default_year=disclosure_date.year,
    )
    return {
        "ratio": extract_split_ratio(text),
        "effective_date": effective_date.isoformat() if effective_date else None,
        "effective_date_raw": raw,
    }
