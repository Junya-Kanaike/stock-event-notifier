from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / "state" / "events.json"
ARCHIVE_DIR = REPO_ROOT / "state" / "archive"
DEFAULT_STATE: dict[str, Any] = {"notified_ids": [], "events": []}


def load_state(path: Path | str = STATE_PATH) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return deepcopy(DEFAULT_STATE)
    with target.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("notified_ids", [])
    data.setdefault("events", [])
    if not isinstance(data["notified_ids"], list) or not isinstance(data["events"], list):
        raise ValueError("Invalid event state schema")
    return data


def save_state(state: dict[str, Any], path: Path | str = STATE_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    tmp.replace(target)


def has_notified(state: dict[str, Any], disclosure_id: str | None) -> bool:
    return bool(disclosure_id) and disclosure_id in set(state.get("notified_ids", []))


def add_notified_id(state: dict[str, Any], disclosure_id: str | None) -> bool:
    if not disclosure_id:
        return False
    ids = state.setdefault("notified_ids", [])
    if disclosure_id in ids:
        return False
    ids.append(disclosure_id)
    return True


def upsert_event(state: dict[str, Any], event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    events = state.setdefault("events", [])
    for index, current in enumerate(events):
        if current.get("id") == event.get("id"):
            if current == event:
                return current, False
            events[index] = event
            return event, True
    events.append(event)
    return event, True


def find_events(
    state: dict[str, Any],
    event_type: str | None = None,
    code: str | None = None,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for event in state.get("events", []):
        if event_type and event.get("type") != event_type:
            continue
        if code and event.get("code") != code:
            continue
        if predicate and not predicate(event):
            continue
        matches.append(event)
    return matches


def mark_schedule_sent(event: dict[str, Any], label: str, sent_date: str) -> bool:
    for item in event.get("schedule", []):
        if item.get("label") == label and item.get("date") == sent_date and not item.get("sent"):
            item["sent"] = True
            return True
    return False


def trim_notified_ids(state: dict[str, Any], limit: int = 5000) -> bool:
    ids = state.setdefault("notified_ids", [])
    if len(ids) <= limit:
        return False
    state["notified_ids"] = ids[-limit:]
    return True


def record_source_result(
    state: dict[str, Any],
    source: str,
    checked_on: date,
    item_count: int,
    *,
    alert_after_empty_days: int = 3,
) -> tuple[bool, bool]:
    health = state.setdefault("source_health", {})
    previous = health.get(source, {})
    current = dict(previous)
    day = checked_on.isoformat()

    if item_count > 0:
        current.update({"last_checked_date": day, "last_success_date": day, "consecutive_empty_days": 0})
    elif previous.get("last_checked_date") != day:
        current.update(
            {
                "last_checked_date": day,
                "consecutive_empty_days": int(previous.get("consecutive_empty_days", 0)) + 1,
            }
        )

    empty_days = int(current.get("consecutive_empty_days", 0))
    should_alert = empty_days >= alert_after_empty_days and int(previous.get("last_alerted_empty_days", 0)) < empty_days
    if should_alert:
        current["last_alerted_empty_days"] = empty_days
    elif item_count > 0:
        current.pop("last_alerted_empty_days", None)

    if current == previous:
        return False, False
    health[source] = current
    return True, should_alert


def archive_completed_events(
    state: dict[str, Any],
    as_of: date,
    *,
    retention_days: int = 30,
    archive_dir: Path | str = ARCHIVE_DIR,
) -> int:
    cutoff = as_of - timedelta(days=retention_days)
    completed: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []

    for event in state.get("events", []):
        schedule = event.get("schedule", [])
        dates = [date.fromisoformat(item["date"]) for item in schedule if item.get("date")]
        if dates and all(item.get("sent") for item in schedule) and max(dates) < cutoff:
            completed.append(event)
        else:
            retained.append(event)

    if not completed:
        return 0

    target_dir = Path(archive_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    by_year: dict[int, list[dict[str, Any]]] = {}
    for event in completed:
        schedule_dates = [date.fromisoformat(item["date"]) for item in event.get("schedule", []) if item.get("date")]
        year = max(schedule_dates).year
        by_year.setdefault(year, []).append(event)

    for year, events in by_year.items():
        target = target_dir / f"events-{year}.json"
        archived: list[dict[str, Any]] = []
        if target.exists():
            with target.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, list):
                raise ValueError(f"Invalid archive schema: {target}")
            archived = loaded
        by_id = {item.get("id"): item for item in archived}
        for event in events:
            by_id[event.get("id")] = event
        _write_json_atomic(target, list(by_id.values()))

    state["events"] = retained
    return len(completed)


def _write_json_atomic(target: Path, data: Any) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    tmp.replace(target)
