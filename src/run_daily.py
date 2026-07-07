from __future__ import annotations

import argparse
from datetime import date
from typing import Any

from src.collectors.jpx_bunbai import fetch_bunbai
from src.collectors.jpx_ipo import fetch_ipos
from src.collectors.jpx_margin import fetch_margin, lookup_margin
from src.collectors.jpx_master import fetch_master, lookup_master
from src.core.bizday import is_business_day, now_jst, today_jst
from src.core.scheduler import build_bunbai_schedule, build_ipo_schedule, due_notifications
from src.core.store import load_state, save_state, upsert_event
from src.notifiers.slack import SlackNotifier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="JST date, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    target_date = date.fromisoformat(args.date) if args.date else today_jst()
    if not is_business_day(target_date):
        print(f"{target_date.isoformat()} is not a business day; skip daily notifications.")
        return 0

    state = load_state()
    master = fetch_master()
    margin = fetch_margin()
    changed = False

    changed |= sync_ipo_events(state, master, margin)
    changed |= sync_bunbai_events(state, master, margin)

    notifier = SlackNotifier(dry_run=args.dry_run)
    for due in due_notifications(state, target_date):
        notifier.send(due.event.get("type", "system"), due.text, pdf_url=due.event.get("pdf_url"))
        due.schedule_item["sent"] = True
        changed = True

    if changed:
        save_state(state)
    return 0


def sync_ipo_events(state: dict[str, Any], master: dict[str, Any], margin: dict[str, str]) -> bool:
    changed = False
    for item in fetch_ipos():
        code = item.get("code")
        listing_date = item.get("listing_date")
        if not code or not listing_date:
            continue
        event_id = f"ipo-{code}-{listing_date}"
        existing = find_event_by_id(state, event_id)
        master_item = lookup_master(master, code, fallback_name=item.get("name", ""))
        event = {
            "id": event_id,
            "type": "ipo",
            "code": code,
            "name": master_item["name"] or item.get("name", ""),
            "market": master_item["market"],
            "margin": lookup_margin(margin, code),
            "announced_at": existing.get("announced_at") if existing else now_jst().isoformat(),
            "detail": {"listing_date": listing_date},
            "schedule": build_ipo_schedule(listing_date, old_schedule=existing.get("schedule") if existing else None),
            "pdf_url": item.get("source_url"),
        }
        _, did_change = upsert_event(state, event)
        changed |= did_change
    return changed


def sync_bunbai_events(state: dict[str, Any], master: dict[str, Any], margin: dict[str, str]) -> bool:
    changed = False
    for item in fetch_bunbai():
        code = item.get("code")
        execution_date = item.get("execution_date")
        if not code or not execution_date:
            continue
        event_id = f"bunbai-{code}-{execution_date}"
        existing = find_event_by_id(state, event_id)
        master_item = lookup_master(master, code, fallback_name=item.get("name", ""))
        event = {
            "id": event_id,
            "type": "bunbai",
            "code": code,
            "name": master_item["name"] or item.get("name", ""),
            "market": master_item["market"],
            "margin": lookup_margin(margin, code),
            "announced_at": existing.get("announced_at") if existing else now_jst().isoformat(),
            "detail": {"execution_date": execution_date, "execution_date_confirmed": True},
            "schedule": build_bunbai_schedule(execution_date, old_schedule=existing.get("schedule") if existing else None),
            "pdf_url": item.get("source_url"),
        }
        _, did_change = upsert_event(state, event)
        changed |= did_change
    return changed


def find_event_by_id(state: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    for event in state.get("events", []):
        if event.get("id") == event_id:
            return event
    return None


if __name__ == "__main__":
    raise SystemExit(main())
