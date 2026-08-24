from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from src.collectors.tdnet import classify_title
from src.core.po import merge_po_details, refresh_po_missing_fields
from src.core.scheduler import build_bunbai_schedule, build_po_schedule, build_split_schedule
from src.parsers.po_pdf import has_ambiguous_settlement_reference


UPDATE_MARKERS = ("訂正", "変更", "終了", "条件決定", "実施に関する")


def reconcile_event_state(state: dict[str, Any]) -> bool:
    """Remove known false positives and consolidate follow-up disclosures."""
    before = deepcopy(state.get("events", []))
    events = [_sanitize_event(_reclassify_event(event)) for event in state.setdefault("events", [])]
    events = [event for event in events if _is_supported_event(event)]
    events = _merge_bunbai_duplicates(events)
    events = _merge_update_duplicates(events, "po")
    events = _merge_update_duplicates(events, "split")
    state["events"] = events
    return events != before


def _reclassify_event(event: dict[str, Any]) -> dict[str, Any]:
    event = deepcopy(event)
    title = event.get("source_title") or ""
    if event.get("type") != "cb" or not title:
        return event
    classes = classify_title(title)
    if "cb" in classes or "split" not in classes:
        return event

    event["type"] = "split"
    event_id = str(event.get("id") or "")
    if event_id.startswith("cb-"):
        event["id"] = f"split-{event_id[3:]}"
    event["detail"] = {
        "ratio": None,
        "effective_date": None,
        "effective_date_raw": None,
        "recovery_needed": True,
        "recovery_reason": "CB転換価額調整として誤分類された株式分割を再抽出",
    }
    event["schedule"] = []
    return event


def _sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    event = deepcopy(event)
    detail = event.setdefault("detail", {})
    if event.get("type") == "split" and str(detail.get("ratio") or "") == "1":
        detail["ratio"] = None
        detail["recovery_needed"] = True
        detail["recovery_reason"] = "末尾0欠落の可能性がある1:1比率を再抽出"

    if event.get("type") == "po" and has_ambiguous_settlement_reference(detail.get("settlement_date_raw")):
        detail["settlement_date"] = None
        detail["settlement_date_end"] = None
        detail["settlement_estimated"] = False
        detail["settlement_date_status"] = "unavailable"
        event["schedule"] = [item for item in event.get("schedule", []) if item.get("label") != "settlement"]
        warnings = detail.setdefault("parse_warnings", [])
        warning = "他の日付を参照する受渡期日を確定日として扱わず除外"
        if warning not in warnings:
            warnings.append(warning)
        refresh_po_missing_fields(detail)
    return event


def _is_supported_event(event: dict[str, Any]) -> bool:
    event_type = event.get("type")
    title = event.get("source_title") or ""
    if event_type not in {"po", "cb"} or not title:
        return True
    classes = classify_title(title)
    if event_type == "po":
        return bool({"po", "po_pricing", "po_correction"} & classes)
    return "cb" in classes


def _merge_bunbai_duplicates(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retained = list(events)
    by_code: dict[str, list[dict[str, Any]]] = {}
    for event in retained:
        if event.get("type") == "bunbai" and event.get("code"):
            by_code.setdefault(str(event["code"]), []).append(event)

    for candidates in by_code.values():
        candidates.sort(key=lambda event: event.get("announced_at", ""))
        primary = candidates[0]
        for duplicate in candidates[1:]:
            if not _same_bunbai_cycle(primary, duplicate):
                primary = duplicate
                continue
            _merge_bunbai_event(primary, duplicate)
            retained.remove(duplicate)
    return retained


def _same_bunbai_cycle(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_detail = first.get("detail", {})
    second_detail = second.get("detail", {})
    first_date = first_detail.get("execution_date")
    second_date = second_detail.get("execution_date")
    if first_date and second_date:
        return first_date == second_date
    if first_detail.get("execution_date_confirmed") and second_detail.get("execution_date_confirmed"):
        return False
    try:
        first_announced = date.fromisoformat(str(first.get("announced_at", ""))[:10])
        second_announced = date.fromisoformat(str(second.get("announced_at", ""))[:10])
    except ValueError:
        return False
    return abs((second_announced - first_announced).days) <= 45


def _merge_bunbai_event(primary: dict[str, Any], duplicate: dict[str, Any]) -> None:
    primary_detail = primary.setdefault("detail", {})
    duplicate_detail = duplicate.get("detail", {})
    execution_confirmed = (
        bool(primary_detail.get("execution_date_confirmed"))
        or bool(duplicate_detail.get("execution_date_confirmed"))
        or any(marker in (duplicate.get("source_title") or "") for marker in ["分売実施", "分売終了", "分売条件"])
    )
    if duplicate_detail.get("execution_date"):
        primary_detail.update(duplicate_detail)
    primary_detail["execution_date_confirmed"] = execution_confirmed
    _merge_event_metadata(primary, duplicate)
    execution_date = primary_detail.get("execution_date")
    if execution_date:
        primary["schedule"] = build_bunbai_schedule(
            execution_date,
            old_schedule=_combined_schedule(primary.get("schedule", []), duplicate.get("schedule", [])),
        )


def _merge_update_duplicates(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    retained = list(events)
    candidates = sorted(
        [event for event in retained if event.get("type") == event_type],
        key=lambda event: event.get("announced_at", ""),
    )
    for duplicate in candidates:
        if duplicate not in retained or not _is_update_title(duplicate.get("source_title", "")):
            continue
        previous = [
            event
            for event in retained
            if event is not duplicate
            and event.get("type") == event_type
            and event.get("code") == duplicate.get("code")
            and event.get("announced_at", "") < duplicate.get("announced_at", "")
        ]
        if not previous:
            continue
        primary = max(previous, key=lambda event: event.get("announced_at", ""))
        if not _within_days(primary, duplicate, 180):
            continue
        if event_type == "po":
            primary["detail"] = merge_po_details(primary.get("detail", {}), duplicate.get("detail", {}))
            detail = primary["detail"]
            if detail.get("pricing_date"):
                primary["schedule"] = build_po_schedule(
                    detail["pricing_date"],
                    detail.get("settlement_date"),
                    old_schedule=_combined_schedule(primary.get("schedule", []), duplicate.get("schedule", [])),
                )
        else:
            primary_detail = primary.setdefault("detail", {})
            for key, value in duplicate.get("detail", {}).items():
                if value is not None:
                    primary_detail[key] = value
            effective_date = primary_detail.get("effective_date")
            if effective_date:
                primary["schedule"] = build_split_schedule(
                    effective_date,
                    old_schedule=_combined_schedule(primary.get("schedule", []), duplicate.get("schedule", [])),
                )
        _merge_event_metadata(primary, duplicate)
        retained.remove(duplicate)
    return retained


def _merge_event_metadata(primary: dict[str, Any], duplicate: dict[str, Any]) -> None:
    if duplicate.get("latest_pdf_url") or duplicate.get("pdf_url"):
        primary["latest_pdf_url"] = duplicate.get("latest_pdf_url") or duplicate.get("pdf_url")
    references = primary.setdefault("related_disclosures", [])
    known_ids = {item.get("id") for item in references}
    duplicate_references = duplicate.get("related_disclosures") or []
    for reference in duplicate_references:
        if reference.get("id") not in known_ids:
            references.append(reference)
            known_ids.add(reference.get("id"))


def _combined_schedule(*schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for schedule in schedules:
        for item in schedule:
            key = (item.get("date"), item.get("label"))
            merged = by_key.setdefault(key, dict(item))
            merged["sent"] = bool(merged.get("sent")) or bool(item.get("sent"))
    return list(by_key.values())


def _is_update_title(title: str) -> bool:
    return any(marker in (title or "") for marker in UPDATE_MARKERS)


def _within_days(first: dict[str, Any], second: dict[str, Any], limit: int) -> bool:
    try:
        first_day = date.fromisoformat(str(first.get("announced_at", ""))[:10])
        second_day = date.fromisoformat(str(second.get("announced_at", ""))[:10])
    except ValueError:
        return False
    return 0 <= (second_day - first_day).days <= limit
