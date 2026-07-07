from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / "state" / "events.json"
DEFAULT_STATE: dict[str, Any] = {"notified_ids": [], "events": []}


def load_state(path: Path | str = STATE_PATH) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return deepcopy(DEFAULT_STATE)
    with target.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("notified_ids", [])
    data.setdefault("events", [])
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
