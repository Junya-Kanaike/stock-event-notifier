"""Persistent evidence for source reads, workflow execution and delivery attempts."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import os
from typing import Any


def stale_sources(state: dict[str, Any], current: datetime) -> list[str]:
    """Cache age is independent of whether a job returned successfully."""
    limits = {"jpx_master": 40, "jpx_margin": 7, "jpx_ipo": 7,
              "jpx_bunbai": 7, "jpx_ex_rights": 7, "traders_split": 7}
    result = []
    for source, limit in limits.items():
        record = state.get("source_health", {}).get(source, {})
        stamp = record.get("last_success_at")
        if not stamp:
            continue
        try:
            last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=current.tzinfo)
            age = (current - last).days
        except (ValueError, TypeError):
            result.append(source + ": 最終取得日時が不正")
            continue
        if age > limit:
            result.append(f"{source}: 最終取得から{age}日（上限{limit}日）")
    return result


def late_delivery_warnings(state: dict[str, Any], current: datetime) -> list[str]:
    result = []
    for event in state.get("events", []):
        if event.get("detail", {}).get("canceled") or event.get("eligibility", {}).get("status") != "eligible":
            continue
        for item in event.get("schedule", []):
            if item.get("sent") or item.get("resolution") or item.get("date") != current.date().isoformat():
                continue
            gate = item.get("not_before_jst", "06:00")
            hour, minute = map(int, gate.split(":"))
            due_at = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if current - due_at >= timedelta(minutes=15):
                result.append(f"{event.get('type')} {event.get('code')} {item.get('label')}: 予定時刻から15分以上未送信")
    return result


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
