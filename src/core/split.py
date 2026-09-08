from __future__ import annotations

from typing import Any


def apply_jpx_ex_right(detail: dict[str, Any], record: dict[str, Any]) -> bool:
    before = dict(detail)
    jpx_date = record.get("rights_final_date")
    if not jpx_date:
        return False
    current = detail.get("rights_final_date")
    detail["jpx_ex_right_date"] = record.get("ex_right_date")
    detail["jpx_rights_final_date"] = jpx_date
    detail["jpx_ex_rights_source_url"] = record.get("source_url")
    if current and current != jpx_date:
        detail["rights_date_conflict"] = True
        detail["rights_final_confirmed"] = False
        detail["rights_final_status"] = "conflict"
    else:
        detail["rights_final_date"] = jpx_date
        detail["ex_right_date"] = record.get("ex_right_date")
        detail["rights_final_source"] = "jpx_ex_rights"
        detail["rights_final_calculation_basis"] = "jpx_ex_right_date_minus_1_business_day"
        detail["rights_final_confirmed"] = True
        detail["rights_final_status"] = "confirmed"
        detail["rights_date_conflict"] = False
    return detail != before


def apply_traders_split(detail: dict[str, Any], record: dict[str, Any]) -> bool:
    """Use Traders Web as the confirmed fallback when TDnet lacks the date."""
    before = dict(detail)
    traders_date = record.get("rights_final_date")
    if not traders_date:
        return False

    current_date = detail.get("rights_final_date")
    current_source = detail.get("rights_final_source")
    detail["traders_rights_final_date"] = traders_date
    detail["traders_split_source_url"] = record.get("source_url")
    if current_date and current_date != traders_date and current_source in {"pdf_explicit", "jpx_ex_rights"}:
        detail["rights_date_conflict"] = True
        detail["rights_final_confirmed"] = False
        detail["rights_final_status"] = "conflict"
    else:
        detail["rights_final_date"] = traders_date
        detail["rights_final_source"] = "traders_web"
        detail["rights_final_calculation_basis"] = "traders_web_rights_final_date"
        detail["rights_final_confirmed"] = True
        detail["rights_final_status"] = "confirmed"
        detail["rights_date_conflict"] = False

    if not detail.get("ratio") and record.get("ratio"):
        detail["ratio"] = record["ratio"]
        detail["ratio_source"] = "traders_web"
    if not detail.get("effective_date") and record.get("effective_date"):
        detail["effective_date"] = record["effective_date"]
    return detail != before
