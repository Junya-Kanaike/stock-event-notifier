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
