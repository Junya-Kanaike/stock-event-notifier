"""Preview/apply audited local state repairs. Never sends notifications."""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

from src.core.bizday import today_jst
from src.core.bizday import now_jst
from src.core.operations import resolve_expired_notification
from src.core.reconcile import reconcile_event_state
from src.core.store import STATE_PATH, load_state, save_state


def repair_report(state, as_of, *, resolve_reviewed_expired=False):
    before = deepcopy(state)
    reconcile_event_state(state, as_of=as_of)
    resolved = []
    if resolve_reviewed_expired:
        # Only the five misses explicitly checked in the October audit.
        reviewed = {"po-3455-2026-08-19": {"2026-08-26", "2026-08-27", "2026-08-28", "2026-09-02"},
                    "po-7966-2026-08-20": {"2026-09-01"}}
        for event in state["events"]:
            for item in event.get("schedule", []):
                if (item.get("date") in reviewed.get(event.get("id"), set())
                        and not item.get("sent") and not item.get("resolution")
                        and (as_of - date.fromisoformat(item["date"])).days > 7):
                    resolve_expired_notification(event, item["date"], item["label"], now_jst(),
                        "2026-10-17監査で未送信を確認。回収期限を超過したため、過去の売買指示を再送せず確認済みとして保管。")
                    resolved.append({"id": event["id"], "date": item["date"], "label": item["label"]})
    old = {event["id"]: event for event in before["events"]}
    changed = [event["id"] for event in state["events"] if event != old.get(event["id"])]
    removed = sorted(set(old) - {event["id"] for event in state["events"]})
    future_flags = [{"id": event["id"], "date": item["date"], "label": item["label"]}
                    for event in state["events"] for item in event.get("schedule", [])
                    if item.get("sent") and not item.get("sent_at") and not item.get("suppressed_reason")
                    and item.get("date", "") > as_of.isoformat()]
    return {"as_of": as_of.isoformat(), "changed": changed, "merged_ids": removed, "reviewed_misses": resolved,
            "event_count": len(state["events"]),
            "eligibility": dict(Counter(e.get("eligibility", {}).get("status") for e in state["events"])),
            "unattributed_future_sent": future_flags,
            "pending": [{"id": e["id"], "reasons": e.get("eligibility", {}).get("reasons")}
                        for e in state["events"] if e.get("eligibility", {}).get("status") == "pending"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--as-of", type=date.fromisoformat, default=today_jst())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resolve-reviewed-expired", action="store_true",
                        help="Record the five audited old misses without claiming delivery")
    args = parser.parse_args(argv)
    state = load_state(args.state)
    before = deepcopy(state)
    report = repair_report(state, args.as_of, resolve_reviewed_expired=args.resolve_reviewed_expired)
    report["applied"] = args.apply
    if args.apply and state != before:
        save_state(state, args.state)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
