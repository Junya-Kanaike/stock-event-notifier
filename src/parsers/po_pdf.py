from __future__ import annotations

import re
from datetime import date
from typing import Any

from src.core.bizday import add_business_days
from src.core.dateparse import clean_text, first_date_near_keywords


AMOUNT_PATTERN = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(億円|百万円|千円|円)")
SIZE_LABELS = [
    "発行価額の総額",
    "発行価額総額",
    "払込金額の総額",
    "売出価額の総額",
    "売出価額総額",
    "オーバーアロットメント",
    "OA",
    "ＯＡ",
]


def classify_po_kind(title: str, text: str = "") -> str:
    combined = clean_text(f"{title} {text[:1000]}")
    has_offering = any(keyword in combined for keyword in ["公募", "新株式発行", "募集株式"])
    has_secondary = "売出" in combined
    if has_offering and has_secondary:
        return "both"
    if has_secondary:
        return "secondary"
    return "offering"


def amount_to_oku(number: str, unit: str) -> float:
    value = float(number.replace(",", ""))
    if unit == "億円":
        return value
    if unit == "百万円":
        return value / 100
    if unit == "千円":
        return value / 100000
    return value / 100000000


def extract_size_oku(text: str) -> float | None:
    normalized = clean_text(text)
    amounts: list[float] = []
    for label in SIZE_LABELS:
        start = normalized.find(label)
        if start < 0:
            continue
        snippet = normalized[start : start + 160]
        match = AMOUNT_PATTERN.search(snippet)
        if match:
            amounts.append(amount_to_oku(match.group(1), match.group(2)))

    if amounts:
        return round(sum(amounts), 2)

    match = re.search(r"(?:約|総額)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*億円", normalized)
    if match:
        return round(float(match.group(1).replace(",", "")), 2)
    return None


def extract_dilution_pct(text: str) -> float | None:
    normalized = clean_text(text)
    patterns = [
        r"希薄化率[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*%",
        r"発行済株式(?:総数)?[^%]{0,80}?([0-9]+(?:\.[0-9]+)?)\s*%",
        r"発行済比[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*%",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return float(match.group(1))
    return None


def parse_po_details(
    title: str,
    text: str,
    disclosure_date: date,
) -> dict[str, Any]:
    default_year = disclosure_date.year
    pricing_date, pricing_raw = first_date_near_keywords(
        text,
        ["発行価格等決定日", "発行価格等の決定日", "価格決定日", "発行価格", "売出価格"],
        default_year=default_year,
        fallback_any=False,
    )
    settlement_date, settlement_raw = first_date_near_keywords(
        text,
        ["受渡期日", "受渡日", "払込期日", "払込日"],
        default_year=default_year,
        fallback_any=False,
    )

    settlement_estimated = False
    if pricing_date and not settlement_date:
        settlement_date = add_business_days(pricing_date, 6)
        settlement_estimated = True

    details: dict[str, Any] = {
        "po_kind": classify_po_kind(title, text),
        "size_oku": extract_size_oku(text),
        "dilution_pct": extract_dilution_pct(text),
        "pricing_date": pricing_date.isoformat() if pricing_date else None,
        "pricing_date_raw": pricing_raw,
        "pricing_date_confirmed": False,
        "settlement_date": settlement_date.isoformat() if settlement_date else None,
        "settlement_date_raw": settlement_raw,
        "settlement_estimated": settlement_estimated,
    }
    return details
