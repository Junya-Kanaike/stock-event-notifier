"""Narrow, idempotent repair of data verified during the September audit."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from src.core.bizday import add_business_days
from src.core.eligibility import apply_eligibility
from src.core.po import refresh_calculated_po_size
from src.core.scheduler import build_po_schedule
from src.parsers.po_pdf import PO_PARSER_VERSION
from src.parsers.split_pdf import extract_record_date


HCM_SOURCE = "https://www.release.tdnet.info/inbs/140120260826526681.pdf"


def repair_verified_event(event: dict[str, Any]) -> None:
    detail = event.setdefault("detail", {})
    if (event.get("id") == "po-3455-2026-08-19"
            and detail.get("pricing_date") == "2026-08-26"
            and not detail.get("canceled")
            and (detail.get("offer_price_yen") == 8836367280
                 or (detail.get("parser_version", 0) < PO_PARSER_VERSION
                     and detail.get("total_offered_shares") is None))):
        # The archived pricing PDF states the unit price and both tranches.
        # Do not override a later pricing correction or an already-correct parse.
        event.setdefault("repair_history", []).append({
            "repair": "2026-09-hcm-unit-price", "source": HCM_SOURCE,
            "previous_detail": deepcopy(detail),
            "previous_schedule": deepcopy(event.get("schedule", [])),
        })
        detail.update({
            "parser_version": PO_PARSER_VERSION, "po_kind": "both",
            "public_offering_shares": 92858, "secondary_sale_shares": 0,
            "oa_shares": 4642, "total_offered_shares": 97500,
            "share_breakdown_complete": True, "oa_marker_present": True,
            "issue_price_yen": 95160, "sale_price_yen": 95160, "offer_price_yen": 95160,
            "pricing_date_confirmed": True, "pricing_date_status": "confirmed",
            "settlement_date": "2026-09-02", "settlement_date_end": None,
            "settlement_date_status": "confirmed", "settlement_estimated": False,
            "verified_calculation_source": HCM_SOURCE,
        })
        refresh_calculated_po_size(detail)
        apply_eligibility(event)
        if event.get("eligibility", {}).get("status") == "eligible":
            event["schedule"] = build_po_schedule("2026-08-26", "2026-09-02",
                                                 old_schedule=event.get("schedule", []))
    if event.get("type") == "split" and detail.get("record_date_raw"):
        # Repair the announcement-date confusion only using the saved official
        # excerpt; do not infer a record date from the effective date.
        try:
            year = date.fromisoformat(str(event.get("announced_at", ""))[:10]).year
            record_date, _ = extract_record_date(detail["record_date_raw"], year)
        except ValueError:
            record_date = None
        if record_date and detail.get("record_date") != record_date.isoformat():
            event.setdefault("repair_history", []).append({
                "repair": "record-date-not-announcement-date",
                "previous_record_date": detail.get("record_date"),
                "source_excerpt": detail["record_date_raw"],
            })
            detail["record_date"] = record_date.isoformat()
            if detail.get("rights_final_confirmed") and detail.get("rights_final_date"):
                detail["ex_right_date"] = add_business_days(detail["rights_final_date"], 1).isoformat()
