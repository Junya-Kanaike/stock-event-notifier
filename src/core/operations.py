"""Persistent evidence for source reads, workflow execution and delivery attempts."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import os
from typing import Any


def record_tdnet_date(state: dict[str, Any], day: date, checked_at: datetime,
                      count: int, error: str | None = None) -> None:
    health = state.setdefault("source_health", {}).setdefault("tdnet", {})
    dates = health.setdefault("by_date", {})
    previous = dates.get(day.isoformat(), {})
    result = {"checked_at": checked_at.isoformat(), "count": count,
              "status": "partial" if error and count else "failed" if error else "success"}
    if error:
        result["error"] = error[:1000]
        if previous.get("last_success_at"):
            result["last_success_at"] = previous["last_success_at"]
    else:
        result["last_success_at"] = checked_at.isoformat()
    dates[day.isoformat()] = result
    health["last_checked_at"] = checked_at.isoformat()
    # Keep failed dates until they have actually been retried successfully.
    for key in sorted(dates):
        if key < (checked_at.date() - timedelta(days=30)).isoformat() and dates[key]["status"] == "success":
            del dates[key]


def record_run(state: dict[str, Any], name: str, started_at: datetime,
               finished_at: datetime, *, failed: bool = False) -> None:
    runs = state.setdefault("workflow_health", {})
    previous = runs.get(name, {})
    record = {"last_started_at": started_at.isoformat(), "last_finished_at": finished_at.isoformat(),
              "status": "failed" if failed else "success", "run_id": os.getenv("GITHUB_RUN_ID", "local")}
    if failed:
        if previous.get("last_success_at"):
            record["last_success_at"] = previous["last_success_at"]
    else:
        record["last_success_at"] = finished_at.isoformat()
    runs[name] = record


def record_delivery(state: dict[str, Any], event: dict[str, Any], item: dict[str, Any],
                    current: datetime, *, success: bool, reference_only: bool = False,
                    error_type: str | None = None) -> None:
    records = state.setdefault("notification_log", [])
    records.append({"event_id": event.get("id"), "channel": event.get("type"),
                    "scheduled_for": item.get("date"), "label": item.get("label"),
                    "attempted_at": current.isoformat(), "outcome": "accepted" if success else "failed",
                    "reference_only": reference_only, "error_type": error_type,
                    "run_id": os.getenv("GITHUB_RUN_ID", "local")})
    del records[:-2000]


def resolve_expired_notification(event: dict[str, Any], day: str, label: str,
                                 current: datetime, reason: str) -> bool:
    """Record a reviewed missed notice, without claiming it was sent."""
    if not reason.strip():
        raise ValueError("A resolution reason is required")
    for item in event.get("schedule", []):
        if (item.get("date"), item.get("label")) != (day, label):
            continue
        if item.get("sent") or (current.date() - date.fromisoformat(day)).days <= 7:
            raise ValueError("Only unsent notifications outside the recovery window can be resolved")
        if item.get("resolution"):
            return False
        item["resolution"] = {"status": "missed_reviewed", "at": current.isoformat(), "reason": reason}
        item.pop("overdue_unresolved", None)
        return True
    raise ValueError("Schedule not found")
