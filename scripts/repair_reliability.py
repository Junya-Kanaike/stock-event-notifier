"""Preview/apply audited local state repairs. Never sends notifications."""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

from src.core.bizday import today_jst
from src.core.reconcile import reconcile_event_state
from src.core.store import STATE_PATH, load_state, save_state


def repair_report(state, as_of):
    before = deepcopy(state)
    reconcile_event_state(state, as_of=as_of)
    old = {event["id"]: event for event in before["events"]}
    changed = [event["id"] for event in state["events"] if event != old.get(event["id"])]
    removed = sorted(set(old) - {event["id"] for event in state["events"]})
    future_flags = [{"id": event["id"], "date": item["date"], "label": item["label"]}
                    for event in state["events"] for item in event.get("schedule", [])
                    if item.get("sent") and not item.get("sent_at") and not item.get("suppressed_reason")
                    and item.get("date", "") > as_of.isoformat()]
    return {"as_of": as_of.isoformat(), "changed": changed, "merged_ids": removed,
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
    args = parser.parse_args(argv)
    state = load_state(args.state)
    before = deepcopy(state)
    report = repair_report(state, args.as_of)
    report["applied"] = args.apply
    if args.apply and state != before:
        save_state(state, args.state)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
