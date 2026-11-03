"""Stable scheduled-notice IDs and an fsynced journal outside the Git state."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def notification_id(event: dict[str, Any], item: dict[str, Any]) -> str:
    identity = [event.get("id"), item.get("date"), item.get("label")]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def write_receipt(event: dict[str, Any], item: dict[str, Any], current: datetime,
                  outcome: str, *, reference_only: bool = False) -> None:
    destination = os.getenv("NOTIFIER_RECEIPTS_PATH")
    if not destination:
        if os.getenv("GITHUB_ACTIONS") == "true":
            raise RuntimeError("NOTIFIER_RECEIPTS_PATH is required before sending scheduled notices")
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": 1, "notification_id": notification_id(event, item),
              "event_id": event.get("id"), "scheduled_for": item.get("date"), "label": item.get("label"),
              "attempted_at": current.isoformat(), "outcome": outcome,
              "reference_only": bool(reference_only), "run_id": os.getenv("GITHUB_RUN_ID", "local")}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def apply_receipts(state: dict[str, Any], records: list[dict[str, Any]], run_id: str) -> int:
    events = {event.get("id"): event for event in state.get("events", [])}
    latest = {}
    for record in records:
        if (record.get("schema_version") != 1 or str(record.get("run_id")) != str(run_id)
                or record.get("outcome") not in {"started", "accepted", "failed", "unknown"}):
            raise ValueError("Invalid delivery receipt")
        identity = notification_id({"id": record.get("event_id")},
                                   {"date": record.get("scheduled_for"), "label": record.get("label")})
        if record.get("notification_id") != identity:
            raise ValueError("Receipt identity does not match its schedule")
        timestamp = datetime.fromisoformat(record["attempted_at"])
        if timestamp.tzinfo is None:
            raise ValueError("Receipt timestamp must have a timezone")
        if latest.get(identity, {}).get("outcome") != "accepted":
            latest[identity] = record
    restored = 0
    for identity, record in latest.items():
        event = events.get(record.get("event_id"))
        if not event:
            continue
        item = next((item for item in event.get("schedule", [])
                     if (item.get("date"), item.get("label")) == (record.get("scheduled_for"), record.get("label"))), None)
        if item is None or item.get("sent") or item.get("resolution") or record["outcome"] == "failed":
            continue
        review = item.get("delivery_review", {})
        if review.get("reviewed_at") and datetime.fromisoformat(review["reviewed_at"]) >= datetime.fromisoformat(record["attempted_at"]):
            continue
        if record["outcome"] == "accepted":
            item.update(sent=True, sent_at=record["attempted_at"], sent_by=str(run_id),
                        reference_only=record.get("reference_only", False), notification_id=identity,
                        restored_from_receipt=True)
            item.pop("delivery_hold", None)
            logs = state.setdefault("notification_log", [])
            if not any(entry.get("outcome") == "accepted" and
                       (entry.get("event_id"), entry.get("scheduled_for"), entry.get("label")) ==
                       (record["event_id"], record["scheduled_for"], record["label"]) for entry in logs):
                logs.append({**record, "channel": event.get("type"), "restored_from_receipt": True})
                del logs[:-2000]
                day = record["attempted_at"][:10]
                stats = state.setdefault("notification_stats", {}).setdefault(day, {"success": 0, "failure": 0})
                stats["success"] = int(stats.get("success", 0)) + 1
        else:
            item["delivery_hold"] = {"status": "unknown", "notification_id": identity,
                                     "attempted_at": record["attempted_at"], "run_id": str(run_id)}
        restored += 1
    return restored


def resolve_delivery_hold(state: dict[str, Any], identity: str, outcome: str,
                          reason: str, current: datetime) -> None:
    if outcome not in {"sent", "not-sent"} or not reason.strip():
        raise ValueError("A confirmed outcome and review reason are required")
    for event in state.get("events", []):
        for item in event.get("schedule", []):
            hold = item.get("delivery_hold", {})
            if hold.get("notification_id") != identity:
                continue
            item["delivery_review"] = {"outcome": outcome, "reason": reason,
                                       "reviewed_at": current.isoformat(), "notification_id": identity}
            if outcome == "sent":
                item.update(sent=True, sent_at=hold["attempted_at"], sent_by=hold["run_id"], notification_id=identity)
            item.pop("delivery_hold")
            return
    raise ValueError("No held schedule matches this notification ID")
