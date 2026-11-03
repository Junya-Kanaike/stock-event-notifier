"""Retain unresolved old POs without retrying expired sources indefinitely."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def is_historical_po(event: dict[str, Any]) -> bool:
    return event.get("type") == "po" and event.get("historical_review", {}).get("status") == "source_unavailable"


def hold_historical_po(event: dict[str, Any], as_of: date) -> None:
    if (event.get("type") != "po" or event.get("eligibility", {}).get("status") != "pending"
            or event.get("schedule") or event.get("detail", {}).get("canceled") or is_historical_po(event)):
        return
    detail = event.get("detail", {})
    if not detail.get("calculation_recovery_last_error"):
        return
    try:
        announced = date.fromisoformat(str(event.get("announced_at", ""))[:10])
        dates = [date.fromisoformat(detail[key]) for key in
                 ("pricing_date", "pricing_date_end", "settlement_date", "settlement_date_end") if detail.get(key)]
    except (ValueError, TypeError):
        return
    # Age alone does not prove an offering has ended. Keep unknown dates and
    # the eligibility decision intact, and require at least one old known date.
    if announced > as_of - timedelta(days=90) or not dates or max(dates) >= as_of - timedelta(days=30):
        return
    sources = {event.get("pdf_url"), event.get("latest_pdf_url")}
    sources.update(item.get("pdf_url") for item in event.get("related_disclosures", []))
    event["historical_review"] = {
        "status": "source_unavailable", "reviewed_on": as_of.isoformat(),
        "reason": "発表から90日以上、既知の日程から30日超経過。元資料を回復できず履歴保留（対象外・送信済みとはしない）",
        "missing_fields": list(detail.get("calculation_missing_fields", [])),
        "source_urls": sorted(source for source in sources if source),
        "automatic_recovery_paused": True,
    }
