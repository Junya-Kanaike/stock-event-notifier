from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
import os
from typing import Any

from src.collectors.jpx_bunbai import fetch_bunbai
from src.collectors.jpx_ipo import fetch_ipos
from src.collectors.jpx_margin import fetch_margin, lookup_margin
from src.collectors.jpx_master import fetch_master, lookup_master
from src.core.bizday import add_business_days, as_date, is_business_day, now_jst, today_jst
from src.core.scheduler import build_bunbai_schedule, build_ipo_schedule, due_notifications
from src.core.store import archive_completed_events, load_state, save_state, upsert_event
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

    if args.dry_run:
        os.environ["CACHE_READ_ONLY"] = "1"

    notifier = SlackNotifier(dry_run=args.dry_run)
    state = load_state()
    if args.dry_run:
        state = deepcopy(state)
    changed = False
    failures: list[str] = []

    try:
        master = fetch_master()
    except Exception as exc:
        master = {}
        failures.append(f"JPX銘柄マスター取得失敗: {exc}")
        notify_system_safely(notifier, failures[-1])

    try:
        margin = fetch_margin()
    except Exception as exc:
        margin = {}
        failures.append(f"JPX信用区分取得失敗: {exc}")
        notify_system_safely(notifier, failures[-1])

    try:
        did_change = sync_ipo_events(state, master, margin, as_of=target_date)
        changed |= did_change
        if did_change and not args.dry_run:
            save_state(state)
    except Exception as exc:
        failures.append(f"IPO同期失敗: {exc}")
        notify_system_safely(notifier, failures[-1])

    try:
        did_change = sync_bunbai_events(state, master, margin, as_of=target_date)
        changed |= did_change
        if did_change and not args.dry_run:
            save_state(state)
    except Exception as exc:
        failures.append(f"立会外分売同期失敗: {exc}")
        notify_system_safely(notifier, failures[-1])

    for due in due_notifications(state, target_date):
        notifier.send(due.event.get("type", "system"), due.text, pdf_url=due.event.get("pdf_url"))
        due.schedule_item["sent"] = True
        changed = True
        if not args.dry_run:
            save_state(state)

    if changed and not args.dry_run:
        save_state(state)
    if not args.dry_run:
        archived = archive_completed_events(state, target_date)
        if archived:
            save_state(state)
    if failures:
        raise RuntimeError("; ".join(failures))
    return 0


def sync_ipo_events(
    state: dict[str, Any],
    master: dict[str, Any],
    margin: dict[str, str],
    *,
    as_of: date | None = None,
) -> bool:
    reference_date = as_of or today_jst()
    changed = False
    for item in fetch_ipos():
        code = item.get("code")
        listing_date = item.get("listing_date")
        if not code or not listing_date:
            continue
        if as_date(listing_date) < reference_date:
            continue
        event_id = f"ipo-{code}-{listing_date}"
        existing = find_event_by_id(state, event_id)
        master_item = lookup_master(master, code, fallback_name=item.get("name", ""))
        market = master_item["market"] if master_item["market"] != "取得失敗" else item.get("market") or "取得失敗"
        event = {
            "id": event_id,
            "type": "ipo",
            "code": code,
            "name": master_item["name"] or item.get("name", ""),
            "market": market,
            "margin": lookup_margin(margin, code),
            "announced_at": existing.get("announced_at") if existing else now_jst().isoformat(),
            "detail": {"listing_date": listing_date},
            "schedule": build_ipo_schedule(listing_date, old_schedule=existing.get("schedule") if existing else None),
            "pdf_url": item.get("source_url"),
        }
        _, did_change = upsert_event(state, event)
        changed |= did_change

    events = state.setdefault("events", [])
    retained = [
        event
        for event in events
        if event.get("type") != "ipo" or not _is_past_ipo_event(event, reference_date)
    ]
    if len(retained) != len(events):
        state["events"] = retained
        changed = True
    return changed


def _is_past_ipo_event(event: dict[str, Any], reference_date: date) -> bool:
    listing_date = event.get("detail", {}).get("listing_date")
    if not listing_date:
        return False
    try:
        return as_date(listing_date) < reference_date
    except (TypeError, ValueError):
        return False


def sync_bunbai_events(
    state: dict[str, Any],
    master: dict[str, Any],
    margin: dict[str, str],
    *,
    as_of: date | None = None,
) -> bool:
    reference_date = as_of or today_jst()
    changed = False
    for item in fetch_bunbai():
        code = item.get("code")
        execution_date = item.get("execution_date")
        if not code or not execution_date:
            continue
        if add_business_days(execution_date, 5) < reference_date:
            continue
        canonical_id = f"bunbai-{code}-{execution_date}"
        existing = find_event_by_id(state, canonical_id) or find_pending_bunbai_event(state, code)
        event_id = existing.get("id") if existing else canonical_id
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


def find_pending_bunbai_event(state: dict[str, Any], code: str) -> dict[str, Any] | None:
    candidates = [
        event
        for event in state.get("events", [])
        if event.get("type") == "bunbai"
        and event.get("code") == code
        and not event.get("detail", {}).get("execution_date_confirmed")
    ]
    return max(candidates, key=lambda event: event.get("announced_at", ""), default=None)


def notify_system_safely(notifier: SlackNotifier, text: str) -> None:
    try:
        notifier.system(text)
    except Exception as exc:  # Avoid masking the source failure or exposing webhook URLs.
        print(f"System alert failed: {type(exc).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
