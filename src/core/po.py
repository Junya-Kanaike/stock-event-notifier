from __future__ import annotations

from copy import deepcopy
from typing import Any


PO_THRESHOLD_YEN = 8_000_000_000


STATUS_LABELS = {
    "confirmed": "確定",
    "estimated": "概算",
    "provisional": "暫定",
    "unavailable": "未取得",
}


def merge_po_details(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(current)
    incoming_stage = incoming.get("source_stage")
    for key, value in incoming.items():
        if key in {
            "source_stage",
            "missing_fields",
            "parse_warnings",
            "size_status",
            "dilution_status",
            "pricing_date_status",
            "settlement_date_status",
        }:
            continue
        if key == "po_kind" and merged.get("po_kind"):
            current_kind = merged["po_kind"]
            if current_kind == "both":
                continue
            if value and value != current_kind:
                merged["po_kind"] = "both"
                continue
        if key == "share_breakdown_complete" and merged.get(key) is True and value is False:
            continue
        if _would_replace_positive_with_zero(key, merged.get(key), value):
            continue
        if value is not None and value != []:
            merged[key] = value

    if incoming.get("size_oku") is not None or (
        incoming.get("size_oku_min") is not None and incoming.get("size_oku_max") is not None
    ):
        merged["size_status"] = incoming.get("size_status") or "estimated"
    if incoming.get("dilution_pct") is not None:
        merged["dilution_status"] = incoming.get("dilution_status") or "confirmed"
    if incoming.get("pricing_date"):
        merged["pricing_date_status"] = incoming.get("pricing_date_status") or "provisional"
    if incoming.get("settlement_date"):
        merged["settlement_date_status"] = incoming.get("settlement_date_status") or "provisional"

    if incoming_stage:
        merged["latest_source_stage"] = incoming_stage
        merged.setdefault("source_stage", incoming_stage)

    merged["pricing_date_confirmed"] = bool(current.get("pricing_date_confirmed")) or bool(
        incoming.get("pricing_date_confirmed")
    )
    merged["po_threshold_ever_met"] = bool(current.get("po_threshold_ever_met")) or bool(
        incoming.get("po_threshold_ever_met")
    )
    if merged["pricing_date_confirmed"]:
        merged["pricing_date_status"] = "confirmed"

    warnings = list(dict.fromkeys([*(current.get("parse_warnings") or []), *(incoming.get("parse_warnings") or [])]))
    merged["parse_warnings"] = warnings
    refresh_po_missing_fields(merged)
    return merged


def refresh_po_missing_fields(detail: dict[str, Any]) -> None:
    missing: list[str] = []
    if detail.get("size_oku") is None and not (
        detail.get("size_oku_min") is not None and detail.get("size_oku_max") is not None
    ):
        missing.append("size")
        detail["size_status"] = "unavailable"
    else:
        detail.setdefault("size_status", "confirmed" if detail.get("size_oku") is not None else "estimated")
    if detail.get("dilution_pct") is None:
        missing.append("dilution_pct")
        detail["dilution_status"] = "unavailable"
    else:
        detail.setdefault("dilution_status", "confirmed")
    if not detail.get("pricing_date"):
        missing.append("pricing_date")
        detail["pricing_date_status"] = "unavailable"
    else:
        detail.setdefault(
            "pricing_date_status", "confirmed" if detail.get("pricing_date_confirmed") else "provisional"
        )
    if not detail.get("settlement_date"):
        missing.append("settlement_date")
        detail["settlement_date_status"] = "unavailable"
    else:
        detail.setdefault(
            "settlement_date_status", "estimated" if detail.get("settlement_estimated") else "confirmed"
        )
    detail["missing_fields"] = missing


def apply_reference_close(detail: dict[str, Any], price: dict[str, Any], reference_kind: str) -> None:
    detail["reference_close_yen"] = float(price["close_yen"])
    detail["reference_close_date"] = price["date"]
    detail["reference_close_source"] = price.get("source")
    detail["reference_close_kind"] = reference_kind
    _refresh_calculated_size(detail)


def refresh_calculated_po_size(detail: dict[str, Any]) -> None:
    _refresh_calculated_size(detail)


def _refresh_calculated_size(detail: dict[str, Any]) -> None:
    total_shares = _positive_int(detail.get("total_offered_shares"))
    calculated_yen: int | None = None
    status = "unavailable"
    basis: str | None = None

    if total_shares and _confirmed_prices_complete(detail):
        public = int(detail.get("public_offering_shares") or 0)
        secondary = int(detail.get("secondary_sale_shares") or 0)
        issue_price = float(detail.get("issue_price_yen") or detail.get("offer_price_yen") or 0)
        sale_price = float(detail.get("sale_price_yen") or detail.get("offer_price_yen") or 0)
        calculated_yen = round(public * issue_price + secondary * sale_price + int(detail.get("oa_shares") or 0) * sale_price)
        detail["confirmed_size_yen"] = calculated_yen
        detail["confirmed_size_oku"] = calculated_yen / 100_000_000
        status = "confirmed"
        basis = "決定価格×（公募株数＋売出株数＋OA株数）"
    elif total_shares and detail.get("reference_close_yen") is not None:
        calculated_yen = round(total_shares * float(detail["reference_close_yen"]))
        detail["estimated_size_yen"] = calculated_yen
        detail["estimated_size_oku"] = calculated_yen / 100_000_000
        status = "provisional" if str(detail.get("reference_close_kind", "")).startswith("previous_close") else "estimated"
        basis = "発表日終値×（公募株数＋売出株数＋OA株数）"

    detail["effective_size_yen"] = calculated_yen
    detail["size_status"] = status
    if calculated_yen is not None:
        detail["size_oku"] = calculated_yen / 100_000_000
        detail["size_basis"] = basis
        if calculated_yen >= PO_THRESHOLD_YEN:
            detail["po_threshold_ever_met"] = True
    else:
        detail.setdefault("po_threshold_ever_met", False)
    refresh_po_missing_fields(detail)


def _confirmed_prices_complete(detail: dict[str, Any]) -> bool:
    kind = detail.get("po_kind")
    issue = detail.get("issue_price_yen") or detail.get("offer_price_yen")
    sale = detail.get("sale_price_yen") or detail.get("offer_price_yen")
    if kind == "offering":
        return issue is not None
    if kind == "secondary":
        return sale is not None
    return issue is not None and sale is not None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _would_replace_positive_with_zero(key: str, current: Any, incoming: Any) -> bool:
    protected = {
        "size_oku",
        "size_oku_min",
        "size_oku_max",
        "public_offering_shares",
        "secondary_sale_shares",
        "oa_shares",
        "total_offered_shares",
        "issue_price_yen",
        "sale_price_yen",
        "offer_price_yen",
    }
    if key not in protected:
        return False
    try:
        return float(current) > 0 and float(incoming) <= 0
    except (TypeError, ValueError):
        return False


def format_po_detail_block(event: dict[str, Any]) -> str:
    detail = event.get("detail", {})
    kind = {"offering": "公募増資", "secondary": "売出し", "both": "公募増資+売出し"}.get(
        detail.get("po_kind"), "要確認"
    )
    lines = [
        f"種別: {kind}",
        f"吸収規模: {_format_size(detail)}",
        f"株数内訳: 公募 {_format_shares(detail.get('public_offering_shares'))} / 売出 {_format_shares(detail.get('secondary_sale_shares'))} / OA {_format_shares(detail.get('oa_shares'))}",
        f"使用価格: {_format_po_price(detail)}",
        f"希薄化率: {_format_dilution(detail)}",
        f"価格決定日: {_format_date_range(detail, 'pricing_date')}",
        f"受渡日: {_format_date_range(detail, 'settlement_date')}",
    ]
    if detail.get("parse_warnings"):
        lines.append("注意: " + " / ".join(detail["parse_warnings"]))
    if detail.get("recovery_notes"):
        lines.append("補完: " + " / ".join(detail["recovery_notes"]))
    return "\n".join(lines)


def format_po_message(event: dict[str, Any], label: str) -> str:
    return (
        f"[{label}] {event.get('code', '')} {event.get('name', '')}"
        f"({event.get('market', '市場不明')} / {event.get('margin', '信用区分不明')})\n"
        f"{format_po_detail_block(event)}"
    )


def _format_size(detail: dict[str, Any]) -> str:
    status = STATUS_LABELS.get(detail.get("size_status"), "未取得")
    if detail.get("size_oku") is not None:
        if detail.get("effective_size_yen") is None:
            return f"約{_number(detail['size_oku'])}億円（開示記載額・株数×価格は未確認）"
        return f"約{_number(detail['size_oku'])}億円（{status}）"
    if detail.get("size_oku_min") is not None and detail.get("size_oku_max") is not None:
        basis = detail.get("size_basis") or "株数×仮条件"
        return f"約{_number(detail['size_oku_min'])}〜{_number(detail['size_oku_max'])}億円（{status}・{basis}）"
    return "取得失敗（PDF要確認）"


def _format_shares(value: Any) -> str:
    return "未取得" if value is None else f"{int(value):,}株"


def _format_po_price(detail: dict[str, Any]) -> str:
    if detail.get("size_status") == "confirmed":
        issue = detail.get("issue_price_yen")
        sale = detail.get("sale_price_yen")
        if issue is not None and sale is not None and issue != sale:
            return f"発行 {_number(issue)}円 / 売出 {_number(sale)}円（決定価格）"
        if detail.get("offer_price_yen") is not None:
            return f"{_number(detail['offer_price_yen'])}円（決定価格）"
    if detail.get("reference_close_yen") is not None:
        return (
            f"{_number(detail['reference_close_yen'])}円"
            f"（{detail.get('reference_close_date', '日付不明')} 終値・{STATUS_LABELS.get(detail.get('size_status'), '概算')}）"
        )
    return "未取得"


def _format_dilution(detail: dict[str, Any]) -> str:
    if detail.get("dilution_pct") is None:
        return "未取得"
    status = STATUS_LABELS.get(detail.get("dilution_status"), "確定")
    return f"{_number(detail['dilution_pct'])}%（{status}）"


def _format_date_range(detail: dict[str, Any], key: str) -> str:
    start = detail.get(key)
    if not start:
        return "未取得"
    end = detail.get(f"{key}_end")
    value = f"{start}〜{end}" if end and end != start else str(start)
    status = STATUS_LABELS.get(detail.get(f"{key}_status"), "暫定")
    return f"{value}（{status}）"


def _number(value: Any) -> str:
    number = float(value)
    return f"{number:,.2f}".rstrip("0").rstrip(".")
