from __future__ import annotations

from typing import Any

from src.core.eligibility import ELIGIBLE, PENDING, apply_eligibility


def eligibility_transition(event: dict[str, Any]) -> str | None:
    apply_eligibility(event)
    detail = event.setdefault("detail", {})
    status = event["eligibility"]["status"]
    if status == PENDING and not detail.get("pending_notified"):
        return "pending"
    if status == ELIGIBLE and not detail.get("eligible_notified"):
        return "confirmed" if detail.get("pending_notified") else "new"
    return None


def mark_transition_notified(event: dict[str, Any], transition: str) -> None:
    detail = event.setdefault("detail", {})
    if transition == "pending":
        detail["pending_notified"] = True
    elif transition in {"new", "confirmed"}:
        detail["eligible_notified"] = True
