from __future__ import annotations

from typing import Any


ELIGIBLE = "eligible"
PENDING = "pending"
EXCLUDED = "excluded"

TSE_PRIME = "prime"
TSE_STANDARD = "standard"
TSE_GROWTH = "growth"


def market_segment(value: str | None) -> str | None:
    normalized = (value or "").strip().lower().replace(" ", "")
    if not normalized or normalized in {"取得失敗", "市場不明", "不明"}:
        return None
    if "pro market" in normalized or "promarket" in normalized or "プロマーケット" in normalized:
        return "pro"
    if "プライム" in normalized or "prime" in normalized:
        return TSE_PRIME
    if "スタンダード" in normalized or "standard" in normalized:
        return TSE_STANDARD
    if "グロース" in normalized or "growth" in normalized:
        return TSE_GROWTH
    return "other"


def evaluate_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    market = market_segment(event.get("market"))
    margin = event.get("margin")
    detail = event.get("detail", {})
    reasons: list[str] = []

    if market is None:
        reasons.append("市場区分未取得")
    elif market == "pro":
        return _result(EXCLUDED, ["PRO Market"])

    if event_type == "ipo":
        if not detail.get("listing_date"):
            reasons.append("上場日未取得")
        return _result(PENDING, reasons) if reasons else _result(ELIGIBLE, [])

    if event_type == "po":
        if margin == "取得失敗":
            reasons.append("信用区分未取得")
        elif margin not in {"貸借", "信用"}:
            return _result(EXCLUDED, ["貸借・信用対象外"])
        if detail.get("po_threshold_ever_met") is True:
            return _result(PENDING, reasons) if reasons else _result(ELIGIBLE, [])
        if detail.get("effective_size_yen") is None:
            reasons.append("吸収規模未確定")
            return _result(PENDING, reasons)
        return _result(EXCLUDED, ["吸収規模80億円未満"])

    if event_type == "bunbai":
        if market not in {TSE_PRIME, TSE_STANDARD, TSE_GROWTH}:
            if market is not None:
                return _result(EXCLUDED, ["東証以外"])
        if margin == "取得失敗":
            reasons.append("信用区分未取得")
        elif margin not in {"貸借", "信用"}:
            return _result(EXCLUDED, ["貸借・信用対象外"])
        if not detail.get("execution_date"):
            reasons.append("分売実施日未取得")
        return _result(PENDING, reasons) if reasons else _result(ELIGIBLE, [])

    if event_type == "cb":
        if margin == "取得失敗":
            reasons.append("信用区分未取得")
        elif margin != "貸借":
            return _result(EXCLUDED, ["貸借対象外"])
        return _result(PENDING, reasons) if reasons else _result(ELIGIBLE, [])

    if event_type == "split":
        if market not in {TSE_PRIME, TSE_STANDARD}:
            if market is not None:
                return _result(EXCLUDED, ["東証Prime・Standard以外"])
        if margin == "取得失敗":
            reasons.append("信用区分未取得")
        elif margin != "貸借":
            return _result(EXCLUDED, ["貸借対象外"])
        if not detail.get("rights_final_date"):
            reasons.append("権利付最終日未取得")
        if detail.get("rights_date_conflict"):
            reasons.append("権利付最終日の照合不一致")
        return _result(PENDING, reasons) if reasons else _result(ELIGIBLE, [])

    return _result(EXCLUDED, ["未対応イベント"])


def apply_eligibility(event: dict[str, Any]) -> bool:
    current = event.get("eligibility")
    evaluated = evaluate_event(event)
    event["eligibility"] = evaluated
    return current != evaluated


def is_eligible(event: dict[str, Any]) -> bool:
    eligibility = event.get("eligibility")
    if not isinstance(eligibility, dict):
        eligibility = evaluate_event(event)
    return eligibility.get("status") == ELIGIBLE


def _result(status: str, reasons: list[str]) -> dict[str, Any]:
    return {"status": status, "reasons": list(dict.fromkeys(reasons))}
