from __future__ import annotations

import re
from datetime import date
from typing import Any

from src.core.bizday import add_business_days, is_business_day, prev_business_day
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
            value = match.group(1)
            return value.rstrip("0").rstrip(".") if "." in value else value
    return None


def parse_split_details(text: str, disclosure_date: date) -> dict[str, Any]:
    rights_final_date, rights_final_raw = first_date_near_keywords(
        text,
        ["権利付最終日", "権利付き最終日"],
        default_year=disclosure_date.year,
        fallback_any=False,
    )
    record_date, record_raw = first_date_near_keywords(
        text,
        ["株式分割の基準日", "分割基準日", "基準日"],
        default_year=disclosure_date.year,
        fallback_any=False,
    )
    effective_date, effective_raw = first_date_near_keywords(
        text,
        ["効力発生日", "効力発生予定日"],
        default_year=disclosure_date.year,
        fallback_any=False,
    )
    source = None
    confirmed = False
    conflict = False
    if rights_final_date:
        source = "pdf_explicit"
        confirmed = is_business_day(rights_final_date)
        conflict = not confirmed
    elif record_date:
        practical_record_date = record_date if is_business_day(record_date) else prev_business_day(record_date)
        rights_final_date = add_business_days(practical_record_date, -2)
        source = "record_date_t_plus_2"
    ex_right_date = add_business_days(rights_final_date, 1) if rights_final_date else None
    return {
        "ratio": extract_split_ratio(text),
        "record_date": record_date.isoformat() if record_date else None,
        "record_date_raw": record_raw,
        "rights_final_date": rights_final_date.isoformat() if rights_final_date else None,
        "rights_final_date_raw": rights_final_raw,
        "rights_final_source": source,
        "rights_final_calculation_basis": source,
        "rights_final_confirmed": confirmed,
        "rights_final_status": "conflict" if conflict else "confirmed" if confirmed else "provisional" if rights_final_date else "unavailable",
        "rights_date_conflict": conflict,
        "ex_right_date": ex_right_date.isoformat() if ex_right_date else None,
        "effective_date": effective_date.isoformat() if effective_date else None,
        "effective_date_raw": effective_raw,
    }
