from __future__ import annotations

from datetime import date
from typing import Any

from src.core.dateparse import first_date_near_keywords


def parse_bunbai_details(text: str, disclosure_date: date) -> dict[str, Any]:
    execution_date, raw = first_date_near_keywords(
        text,
        ["分売実施日", "分売実施予定日", "実施予定日", "分売予定期間"],
        default_year=disclosure_date.year,
        fallback_any=False,
    )
    return {
        "execution_date": execution_date.isoformat() if execution_date else None,
        "execution_date_raw": raw,
        "execution_date_confirmed": False,
    }
