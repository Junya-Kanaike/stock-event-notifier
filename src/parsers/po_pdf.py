from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from src.core.bizday import add_business_days
from src.core.dateparse import clean_text, find_dates, first_date_near_keywords, normalize_digits


AMOUNT_PATTERN = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(億円|百万円|千円|円)")
SIZE_LABELS = [
    "発行価額の総額",
    "発行価額総額",
    "払込金額の総額",
    "払込金額（発行価額）の総額",
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
    amounts_by_span: dict[tuple[int, int], float] = {}
    for label in SIZE_LABELS:
        start = normalized.find(label)
        if start < 0:
            continue
        snippet = normalized[start : start + 160]
        match = AMOUNT_PATTERN.search(snippet)
        if match:
            span = (start + match.start(), start + match.end())
            amounts_by_span[span] = amount_to_oku(match.group(1), match.group(2))

    if amounts_by_span:
        return round(sum(amounts_by_span.values()), 2)

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


def extract_estimated_size(text: str) -> dict[str, Any] | None:
    compact = re.sub(r"\s+", "", normalize_digits(text))
    price_match = re.search(
        r"(?:1株につき|1口当たり)([0-9][0-9,]*(?:\.[0-9]+)?)円から([0-9][0-9,]*(?:\.[0-9]+)?)円",
        compact,
    )
    if not price_match:
        return None

    base_patterns = [
        r"合計による(?:当社)?普通株式([0-9][0-9,]*)株",
        r"募集(?:株式|投資口)数([0-9][0-9,]*)(?:株|口)",
        r"売出株式数([0-9][0-9,]*)株",
    ]
    base_match = None
    for pattern in base_patterns:
        base_match = re.search(pattern, compact)
        if base_match:
            break
    if not base_match:
        return None

    oa_match = re.search(
        r"オーバーアロットメントによる売出し.{0,350}?売出株式の種類及び数(?:当社)?普通株式([0-9][0-9,]*)株",
        compact,
    )
    share_count = int(base_match.group(1).replace(",", ""))
    oa_share_count = int(oa_match.group(1).replace(",", "")) if oa_match else 0
    price_min = float(price_match.group(1).replace(",", ""))
    price_max = float(price_match.group(2).replace(",", ""))
    total_shares = share_count + oa_share_count
    return {
        "size_oku_min": round(total_shares * price_min / 100_000_000, 2),
        "size_oku_max": round(total_shares * price_max / 100_000_000, 2),
        "share_count": share_count,
        "oa_share_count": oa_share_count,
        "price_min_yen": price_min,
        "price_max_yen": price_max,
        "size_basis": "株数（OA上限込み）×仮条件",
    }


def classify_source_stage(title: str) -> str:
    normalized = re.sub(r"\s+", "", title or "")
    if "訂正" in normalized:
        return "correction"
    if "仮条件" in normalized:
        return "preliminary"
    if "決定" in normalized and any(keyword in normalized for keyword in ["発行価格", "売出価格", "価格等"]):
        return "pricing"
    return "announcement"


def has_confirmed_price(text: str) -> bool:
    compact = re.sub(r"\s+", "", normalize_digits(text))
    price_is_stated = bool(
        re.search(r"(?:発行価格|売出価格|募集価格)(?:1株につき|1口当たり)?[0-9][0-9,]*(?:\.[0-9]+)?円", compact)
    )
    return price_is_stated and "決定" in compact and "仮条件" not in compact


def _range_end(raw: str | None, start: date | None, default_year: int) -> date | None:
    if not raw or not start:
        return None
    if not any(marker in raw for marker in ["から", "〜", "～"]):
        return None
    for candidate in find_dates(raw, default_year=default_year)[1:]:
        if start <= candidate <= start + timedelta(days=60):
            return candidate
    return None


def refresh_missing_fields(detail: dict[str, Any]) -> None:
    missing: list[str] = []
    if detail.get("size_status") == "unavailable":
        missing.append("size")
    if detail.get("dilution_pct") is None:
        missing.append("dilution_pct")
    if not detail.get("pricing_date"):
        missing.append("pricing_date")
    if not detail.get("settlement_date"):
        missing.append("settlement_date")
    detail["missing_fields"] = missing


def parse_po_details(
    title: str,
    text: str,
    disclosure_date: date,
) -> dict[str, Any]:
    default_year = disclosure_date.year
    source_stage = classify_source_stage(title)
    pricing_date, pricing_raw = first_date_near_keywords(
        text,
        ["発行価格等決定日", "発行価格等の決定日", "価格決定日", "発行価格", "売出価格"],
        default_year=default_year,
        fallback_any=False,
    )
    if source_stage == "pricing":
        # 価格決定資料の本文には決定日が明記されないことが多い。
        # 開示日時を確定日として扱うのは呼び出し側の責務とする。
        pricing_date = None
        pricing_raw = None
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

    size_oku = extract_size_oku(text)
    estimated_size = extract_estimated_size(text) if size_oku is None else None
    pricing_date_end = _range_end(pricing_raw, pricing_date, default_year)
    settlement_date_end = _range_end(settlement_raw, settlement_date, default_year)
    size_status = "confirmed" if size_oku is not None else "estimated" if estimated_size else "unavailable"
    if size_oku is not None and "概算" in clean_text(text):
        size_status = "estimated"
    pricing_confirmed = bool(pricing_date and has_confirmed_price(text))
    settlement_is_provisional = bool(settlement_date_end or (settlement_raw and "予定" in settlement_raw))

    dilution_pct = extract_dilution_pct(text)
    details: dict[str, Any] = {
        "po_kind": classify_po_kind(title, text),
        "source_stage": source_stage,
        "size_oku": size_oku,
        "size_oku_min": estimated_size.get("size_oku_min") if estimated_size else None,
        "size_oku_max": estimated_size.get("size_oku_max") if estimated_size else None,
        "size_status": size_status,
        "size_basis": "開示資料記載額" if size_oku is not None else estimated_size.get("size_basis") if estimated_size else None,
        "share_count": estimated_size.get("share_count") if estimated_size else None,
        "oa_share_count": estimated_size.get("oa_share_count") if estimated_size else None,
        "price_min_yen": estimated_size.get("price_min_yen") if estimated_size else None,
        "price_max_yen": estimated_size.get("price_max_yen") if estimated_size else None,
        "dilution_pct": dilution_pct,
        "dilution_status": "confirmed" if dilution_pct is not None else "unavailable",
        "pricing_date": pricing_date.isoformat() if pricing_date else None,
        "pricing_date_end": pricing_date_end.isoformat() if pricing_date_end else None,
        "pricing_date_raw": pricing_raw,
        "pricing_date_confirmed": pricing_confirmed,
        "pricing_date_status": "confirmed" if pricing_confirmed else "provisional" if pricing_date else "unavailable",
        "settlement_date": settlement_date.isoformat() if settlement_date else None,
        "settlement_date_end": settlement_date_end.isoformat() if settlement_date_end else None,
        "settlement_date_raw": settlement_raw,
        "settlement_estimated": settlement_estimated,
        "settlement_date_status": "estimated" if settlement_estimated else "provisional" if settlement_is_provisional else "confirmed" if settlement_date else "unavailable",
        "parse_warnings": [],
    }
    refresh_missing_fields(details)
    return details
