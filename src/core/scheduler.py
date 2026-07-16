from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from src.core.bizday import add_business_days, as_date, prev_business_day


@dataclass
class DueNotification:
    event: dict[str, Any]
    schedule_item: dict[str, Any]
    text: str
    scheduled_for: date
    overdue: bool = False


def _entry(day: date, label: str, sent: bool = False) -> dict[str, Any]:
    return {"date": day.isoformat(), "label": label, "sent": sent}


def _merge_sent(old: list[dict[str, Any]] | None, new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sent_by_key = {
        (item.get("date"), item.get("label")): bool(item.get("sent"))
        for item in old or []
    }
    for item in new:
        item["sent"] = sent_by_key.get((item.get("date"), item.get("label")), False)
    return new


def build_po_schedule(
    pricing_date: date | str,
    settlement_date: date | str | None,
    old_schedule: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    pricing = as_date(pricing_date)
    schedule = [
        _entry(pricing, "pricing_day"),
        _entry(add_business_days(pricing, 1), "pricing_day+1"),
        _entry(add_business_days(pricing, 2), "pricing_day+2"),
    ]
    if settlement_date:
        schedule.append(_entry(as_date(settlement_date), "settlement"))
    schedule.extend(
        [
            _entry(add_business_days(pricing, 25), "pricing_day+25bd"),
            _entry(add_business_days(pricing, 26), "pricing_day+26bd"),
        ]
    )
    return _merge_sent(old_schedule, schedule)


def build_ipo_schedule(listing_date: date | str, old_schedule: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    listing = as_date(listing_date)
    return _merge_sent(old_schedule, [_entry(prev_business_day(listing), "listing-1bd"), _entry(listing, "listing_day")])


def build_bunbai_schedule(
    execution_date: date | str,
    old_schedule: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    execution = as_date(execution_date)
    return _merge_sent(
        old_schedule,
        [
            _entry(prev_business_day(execution), "execution-1bd"),
            _entry(execution, "execution_day"),
            _entry(add_business_days(execution, 5), "execution+5bd"),
        ],
    )


def build_split_schedule(
    effective_date: date | str,
    old_schedule: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    effective = as_date(effective_date)
    return _merge_sent(old_schedule, [_entry(prev_business_day(effective), "effective-1bd"), _entry(effective, "effective_day")])


def render_daily_message(event: dict[str, Any], label: str) -> str:
    code = event.get("code", "")
    name = event.get("name", "")
    market = event.get("market", "市場不明")
    display = f"{code} {name}".strip()
    detail = event.get("detail", {})

    if event.get("type") == "po":
        if label == "pricing_day":
            return f"[PO] 本日は {display} の価格決定日です。*寄り付きで買う*"
        if label == "pricing_day+1":
            return f"[PO] 本日は {display} の価格決定日の翌営業日です。*寄り付きで買う*"
        if label == "pricing_day+2":
            return f"[PO] 本日は {display} の価格決定日の翌々営業日です。*寄り付きで買う*"
        if label == "settlement":
            return f"[PO] 本日は {display} の受渡日です。*寄り付きで買う*"
        if label == "pricing_day+25bd":
            return f"[PO] 本日は {display} の価格決定日から25営業日後です"
        if label == "pricing_day+26bd":
            return f"[PO] 本日は {display} の価格決定日から26営業日後です"

    if event.get("type") == "ipo":
        if label == "listing-1bd":
            return f"[IPO] 明日 {display} が新規上場します"
        if label == "listing_day":
            return f"[IPO] 本日 {display} が新規上場します"

    if event.get("type") == "bunbai":
        if label == "execution-1bd":
            return f"[立会外分売] 明日 {display} の立会外分売が実施予定です"
        if label == "execution_day":
            return f"[立会外分売] 本日 {display} の立会外分売実施日です。*寄り付きで買う*"
        if label == "execution+5bd":
            return f"[立会外分売] {display}(分売): 本日実施日から5営業日後です。*売却する*"

    if event.get("type") == "split":
        ratio = detail.get("ratio") or "要確認"
        suffix = f"{display}({market})の株式分割(1:{ratio})"
        if label == "effective-1bd":
            return f"[株式分割] 明日 {suffix}の効力発生日です"
        if label == "effective_day":
            return f"[株式分割] 本日 {suffix}の効力発生日です"

    return f"[{event.get('type', 'event')}] {display}: {label}"


def due_notifications(state: dict[str, Any], today: date | str) -> list[DueNotification]:
    target_date = as_date(today)
    due: list[DueNotification] = []
    for event in state.get("events", []):
        if event.get("detail", {}).get("canceled"):
            continue
        for item in event.get("schedule", []):
            if item.get("sent") or not item.get("date"):
                continue
            scheduled_for = as_date(item["date"])
            if scheduled_for > target_date:
                continue
            overdue = scheduled_for < target_date
            text = render_daily_message(event, item.get("label", ""))
            if overdue:
                text = (
                    f"⚠️ [遅延通知] 本来の通知日: {scheduled_for.isoformat()}\n"
                    f"{text}\n"
                    "※以下の日時表現と売買指示は本来の通知日時点の内容で、現在時点の指示ではありません。"
                )
            due.append(DueNotification(event, item, text, scheduled_for, overdue))
    return sorted(due, key=lambda item: (item.scheduled_for, item.event.get("id", ""), item.schedule_item.get("label", "")))
