from __future__ import annotations

from copy import deepcopy
from typing import Any


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


def format_po_detail_block(event: dict[str, Any]) -> str:
    detail = event.get("detail", {})
    kind = {"offering": "公募増資", "secondary": "売出し", "both": "公募増資+売出し"}.get(
        detail.get("po_kind"), "要確認"
    )
    lines = [
        f"種別: {kind}",
        f"吸収規模: {_format_size(detail)}",
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
        return f"約{_number(detail['size_oku'])}億円（{status}）"
    if detail.get("size_oku_min") is not None and detail.get("size_oku_max") is not None:
        basis = detail.get("size_basis") or "株数×仮条件"
        return f"約{_number(detail['size_oku_min'])}〜{_number(detail['size_oku_max'])}億円（{status}・{basis}）"
    return "取得失敗（PDF要確認）"


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
