from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from src.core.bizday import JST, add_business_days, as_date, prev_business_day
from src.core.eligibility import is_eligible
from src.core.po import format_po_detail_block


@dataclass
class DueNotification:
    event: dict[str, Any]
    schedule_item: dict[str, Any]
    text: str
    scheduled_for: date
    overdue: bool = False


def _entry(
    day: date,
    label: str,
    sent: bool = False,
    *,
    not_before_jst: str | None = None,
    action_cutoff_jst: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"date": day.isoformat(), "label": label, "sent": sent}
    if not_before_jst:
        item["not_before_jst"] = not_before_jst
    if action_cutoff_jst:
        item["action_cutoff_jst"] = action_cutoff_jst
    return item


def _merge_sent(old: list[dict[str, Any]] | None, new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sent_by_key = {
        (item.get("date"), item.get("label")): bool(item.get("sent"))
        for item in old or []
    }
    sent_labels = {item.get("label") for item in old or [] if item.get("sent")}
    for item in new:
        item["sent"] = sent_by_key.get(
            (item.get("date"), item.get("label")),
            item.get("label") in sent_labels,
        )
    return new


def build_po_schedule(
    pricing_date: date | str,
    settlement_date: date | str | None,
    old_schedule: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    pricing = as_date(pricing_date)
    schedule = [
        _entry(pricing, "pricing_day", action_cutoff_jst="09:00"),
        _entry(add_business_days(pricing, 1), "pricing_day+1", action_cutoff_jst="09:00"),
        _entry(add_business_days(pricing, 2), "pricing_day+2", action_cutoff_jst="09:00"),
    ]
    if settlement_date:
        schedule.append(_entry(as_date(settlement_date), "settlement", action_cutoff_jst="09:00"))
    schedule.extend(
        [
            _entry(add_business_days(pricing, 25), "pricing_day+25bd", action_cutoff_jst="09:00"),
            _entry(add_business_days(pricing, 26), "pricing_day+26bd", action_cutoff_jst="09:00"),
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
            _entry(execution, "execution_day", action_cutoff_jst="09:00"),
            _entry(add_business_days(execution, 5), "execution+5bd", action_cutoff_jst="09:00"),
        ],
    )


def build_split_schedule(
    rights_final_date: date | str,
    old_schedule: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rights_final = as_date(rights_final_date)
    return _merge_sent(
        old_schedule,
        [
            _entry(rights_final, "rights_final+0_after_close", not_before_jst="19:00"),
            _entry(
                add_business_days(rights_final, 1),
                "rights_final+1_noon",
                not_before_jst="12:00",
                action_cutoff_jst="15:30",
            ),
            _entry(
                add_business_days(rights_final, 5),
                "rights_final+5_preopen",
                not_before_jst="08:00",
                action_cutoff_jst="09:00",
            ),
        ],
    )


def render_daily_message(event: dict[str, Any], label: str, *, reference_only: bool = False) -> str:
    code = event.get("code", "")
    name = event.get("name", "")
    market = event.get("market", "市場不明")
    display = f"{code} {name}".strip()
    detail = event.get("detail", {})

    if event.get("type") == "po":
        if label == "pricing_day":
            return _po_daily(event, _action_text(f"[PO] 本日は {display} の価格決定日です。", "寄り付きで買う", reference_only))
        if label == "pricing_day+1":
            return _po_daily(event, _action_text(f"[PO] 本日は {display} の価格決定日の翌営業日です。", "寄り付きで買う", reference_only))
        if label == "pricing_day+2":
            return _po_daily(event, _action_text(f"[PO] 本日は {display} の価格決定日の翌々営業日です。", "寄り付きで買う", reference_only))
        if label == "settlement":
            return _po_daily(event, _action_text(f"[PO] 本日は {display} の受渡日です。", "寄り付きで買う", reference_only))
        if label == "pricing_day+25bd":
            return _po_daily(event, _action_text(f"[PO] 本日は {display} の価格決定日から25営業日後です。", "寄り付きで半分売る", reference_only))
        if label == "pricing_day+26bd":
            return _po_daily(event, _action_text(f"[PO] 本日は {display} の価格決定日から26営業日後です。", "寄り付きで半分売る", reference_only))

    if event.get("type") == "ipo":
        if label == "listing-1bd":
            return f"[IPO] 明日 {display} が新規上場します"
        if label == "listing_day":
            return f"[IPO] 本日 {display} が新規上場します"

    if event.get("type") == "bunbai":
        if label == "execution-1bd":
            return f"[立会外分売] 明日 {display} の立会外分売が実施予定です。申し込み忘れずに！"
        if label == "execution_day":
            return _action_text(f"[立会外分売] 本日 {display} の立会外分売実施日です。", "寄り付きで買う", reference_only)
        if label == "execution+5bd":
            return _action_text(f"[立会外分売] {display}(分売): 本日実施日から5営業日後です。", "寄り付きで売却する", reference_only)

    if event.get("type") == "split":
        ratio = detail.get("ratio") or "要確認"
        suffix = f"{display}({market})の株式分割(1:{ratio})"
        if label == "rights_final+0_after_close":
            return f"[株式分割] {suffix}: *分割の空売り検討*" if not reference_only else f"[株式分割] {suffix}: 権利付最終日の参考通知"
        if label == "rights_final+1_noon":
            return _action_text(f"[株式分割] {suffix}: 権利付最終日の翌営業日です。", "大引で空売り", reference_only)
        if label == "rights_final+5_preopen":
            return _action_text(f"[株式分割] {suffix}: 権利付最終日から5営業日後です。", "寄り付きで買い戻し", reference_only)

    return f"[{event.get('type', 'event')}] {display}: {label}"


def _po_daily(event: dict[str, Any], action: str) -> str:
    return f"{action}\n{format_po_detail_block(event)}"


def _action_text(prefix: str, action: str, reference_only: bool) -> str:
    return f"{prefix}（参考通知）" if reference_only else f"{prefix} *{action}*"


def is_po_awaiting_pricing(event: dict[str, Any]) -> bool:
    """Return whether a PO still lacks a confirmed pricing disclosure."""
    if event.get("type") != "po":
        return False
    return event.get("detail", {}).get("pricing_date_confirmed") is not True


def due_notifications(state: dict[str, Any], now: date | datetime | str) -> list[DueNotification]:
    target_date, target_time = _target_date_time(now)
    due: list[DueNotification] = []
    for event in state.get("events", []):
        if event.get("detail", {}).get("canceled"):
            continue
        if isinstance(event.get("eligibility"), dict) and not is_eligible(event):
            continue
        for item in event.get("schedule", []):
            if item.get("sent") or not item.get("date"):
                continue
            scheduled_for = as_date(item["date"])
            if scheduled_for > target_date:
                continue
            if (target_date - scheduled_for).days > 7:
                item["overdue_unresolved"] = True
                continue
            overdue = scheduled_for < target_date
            not_before = _parse_time(item.get("not_before_jst"))
            if not overdue and not_before and target_time < not_before:
                continue
            cutoff = _parse_time(item.get("action_cutoff_jst"))
            reference_only = overdue or bool(cutoff and target_time >= cutoff)
            text = render_daily_message(event, item.get("label", ""), reference_only=reference_only)
            if overdue:
                text = (
                    f"⚠️ [遅延通知] 本来の通知日: {scheduled_for.isoformat()}\n"
                    f"{text}\n"
                    "※以下の日時表現と売買指示は本来の通知日時点の内容で、現在時点の指示ではありません。"
                )
            due.append(DueNotification(event, item, text, scheduled_for, overdue))
    return sorted(due, key=lambda item: (item.scheduled_for, item.event.get("id", ""), item.schedule_item.get("label", "")))


def _target_date_time(value: date | datetime | str) -> tuple[date, time]:
    if isinstance(value, datetime):
        current = value.astimezone(JST) if value.tzinfo else value.replace(tzinfo=JST)
        return current.date(), current.time().replace(tzinfo=None)
    return as_date(value), time(7, 30)


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    hour, minute = str(value).split(":", 1)
    return time(int(hour), int(minute))
