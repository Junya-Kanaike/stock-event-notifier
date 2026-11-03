"""Read-only health checks shared by hosted jobs and the standalone Mac helper."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def stale_sources(state: dict[str, Any], current: datetime) -> list[str]:
    """Cache age is independent of whether a job returned successfully."""
    limits = {"jpx_master": 40, "jpx_margin": 7, "jpx_ipo": 7,
              "jpx_bunbai": 7, "jpx_ex_rights": 7, "traders_split": 7}
    result = []
    for source, limit in limits.items():
        record = state.get("source_health", {}).get(source, {})
        stamp = record.get("last_success_at")
        if not stamp:
            continue
        try:
            last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=current.tzinfo)
            age = (current - last).days
        except (ValueError, TypeError):
            result.append(source + ": 最終取得日時が不正")
            continue
        if age > limit:
            result.append(f"{source}: 最終取得から{age}日（上限{limit}日）")
    return result


def late_delivery_warnings(state: dict[str, Any], current: datetime) -> list[str]:
    result = []
    for event in state.get("events", []):
        if event.get("detail", {}).get("canceled") or event.get("eligibility", {}).get("status") != "eligible":
            continue
        for item in event.get("schedule", []):
            if item.get("sent") or item.get("resolution"):
                continue
            if item.get("delivery_hold"):
                result.append(f"{event.get('type')} {event.get('code')} {item.get('label')}: 送信結果不明・自動再送停止（Slack照合が必要）")
                continue
            if item.get("date") != current.date().isoformat():
                continue
            gate = item.get("not_before_jst", "06:00")
            hour, minute = map(int, gate.split(":"))
            due_at = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if current - due_at >= timedelta(minutes=15):
                result.append(f"{event.get('type')} {event.get('code')} {item.get('label')}: 予定時刻から15分以上未送信")
    return result
