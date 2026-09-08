from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, time
import os
from typing import Any

from src.collectors.jpx_bunbai import CACHE_NAME as BUNBAI_CACHE_NAME, fetch_bunbai
from src.collectors.jpx_ex_rights import CACHE_NAME as EX_RIGHTS_CACHE_NAME, fetch_ex_rights
from src.collectors.jpx_ipo import CACHE_NAME as IPO_CACHE_NAME, fetch_ipos
from src.collectors.jpx_margin import CACHE_NAME as MARGIN_CACHE_NAME, fetch_margin, lookup_margin
from src.collectors.jpx_master import CACHE_NAME as MASTER_CACHE_NAME, fetch_master, lookup_master
from src.collectors.utils import cache_fetched_at
from src.core.bizday import add_business_days, as_date, is_business_day, now_jst, today_jst
from src.core.eligibility import ELIGIBLE, PENDING
from src.core.reconcile import reconcile_event_state
from src.core.scheduler import build_bunbai_schedule, build_ipo_schedule, due_notifications
from src.core.store import (
    archive_completed_events,
    load_state,
    record_notification_counts,
    record_source_success,
    save_state,
    upsert_event,
)
from src.core.transitions import eligibility_transition, mark_transition_notified
from src.notifiers.slack import SlackNotifier
from src.run_poll import reconcile_split_ex_rights


SYSTEM_SUMMARY_AFTER = time(20, 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="JST date, YYYY-MM-DD")
    parser.add_argument("--now", help="JST datetime for scheduled dispatch, ISO-8601")
    parser.add_argument("--notifications-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    current = _resolve_now(args.now, args.date)
    target_date = current.date()

    if args.dry_run:
        os.environ["CACHE_READ_ONLY"] = "1"

    notifier = SlackNotifier(dry_run=args.dry_run)
    try:
        state = load_state()
        if args.dry_run:
            state = deepcopy(state)
        changed = reconcile_event_state(state)
    except Exception as exc:
        notify_system_safely(notifier, f"state整合性エラー: {type(exc).__name__}: {exc}")
        raise
    if changed and not args.dry_run:
        save_state(state)
    failures: list[str] = []

    if not is_business_day(target_date):
        if not args.notifications_only:
            changed |= send_daily_system_summary(state, notifier, current, failures=[])
            changed |= record_notification_counts(
                state,
                target_date,
                success_count=notifier.success_count,
                failure_count=notifier.failure_count,
            )
            if changed and not args.dry_run:
                save_state(state)
        print(f"{target_date.isoformat()} is not a business day; event notifications skipped.")
        return 0

    if args.notifications_only:
        sent_count = send_due_notifications(state, notifier, current, dry_run=args.dry_run)
        changed = bool(sent_count)
        if current.time().replace(tzinfo=None) >= SYSTEM_SUMMARY_AFTER:
            changed |= send_daily_system_summary(state, notifier, current, failures=[])
        changed |= record_notification_counts(
            state,
            target_date,
            success_count=notifier.success_count,
            failure_count=notifier.failure_count,
        )
        if changed and not args.dry_run:
            save_state(state)
        return 0

    master_cache_before = cache_fetched_at(MASTER_CACHE_NAME)
    try:
        master = fetch_master(force=True)
        changed |= record_cached_source_success(state, "jpx_master", MASTER_CACHE_NAME)
        report_cache_refresh(notifier, "JPX銘柄マスター", MASTER_CACHE_NAME, master_cache_before, dry_run=args.dry_run)
    except Exception as exc:
        master = {}
        failures.append(f"JPX銘柄マスター取得失敗: {exc}")
        notify_system_safely(notifier, failures[-1])

    margin_cache_before = cache_fetched_at(MARGIN_CACHE_NAME)
    try:
        margin = fetch_margin(force=True)
        changed |= record_cached_source_success(state, "jpx_margin", MARGIN_CACHE_NAME)
        report_cache_refresh(notifier, "JPX信用区分", MARGIN_CACHE_NAME, margin_cache_before, dry_run=args.dry_run)
    except Exception as exc:
        margin = {}
        failures.append(f"JPX信用区分取得失敗: {exc}")
        notify_system_safely(notifier, failures[-1])

    ex_rights_cache_before = cache_fetched_at(EX_RIGHTS_CACHE_NAME)
    try:
        ex_rights = fetch_ex_rights(force=True)
        changed |= record_cached_source_success(state, "jpx_ex_rights", EX_RIGHTS_CACHE_NAME)
        report_cache_refresh(
            notifier,
            "JPX権利落情報",
            EX_RIGHTS_CACHE_NAME,
            ex_rights_cache_before,
            dry_run=args.dry_run,
        )
        changed |= reconcile_split_ex_rights(state, ex_rights, notifier)
    except Exception as exc:
        failures.append(f"JPX権利落情報取得失敗: {exc}")
        notify_system_safely(notifier, failures[-1])

    ipo_cache_before = cache_fetched_at(IPO_CACHE_NAME)
    try:
        did_change = sync_ipo_events(
            state, master, margin, as_of=target_date, force_refresh=True, notifier=notifier
        )
        changed |= record_cached_source_success(state, "jpx_ipo", IPO_CACHE_NAME)
        report_cache_refresh(notifier, "JPX IPO", IPO_CACHE_NAME, ipo_cache_before, dry_run=args.dry_run)
        changed |= did_change
        if did_change and not args.dry_run:
            save_state(state)
    except Exception as exc:
        failures.append(f"IPO同期失敗: {exc}")
        notify_system_safely(notifier, failures[-1])

    bunbai_cache_before = cache_fetched_at(BUNBAI_CACHE_NAME)
    try:
        did_change = sync_bunbai_events(
            state, master, margin, as_of=target_date, force_refresh=True, notifier=notifier
        )
        changed |= record_cached_source_success(state, "jpx_bunbai", BUNBAI_CACHE_NAME)
        report_cache_refresh(notifier, "JPX立会外分売", BUNBAI_CACHE_NAME, bunbai_cache_before, dry_run=args.dry_run)
        changed |= did_change
        if did_change and not args.dry_run:
            save_state(state)
    except Exception as exc:
        failures.append(f"立会外分売同期失敗: {exc}")
        notify_system_safely(notifier, failures[-1])

    sent_count = send_due_notifications(state, notifier, current, dry_run=args.dry_run)
    changed |= bool(sent_count)
    if current.time().replace(tzinfo=None) >= SYSTEM_SUMMARY_AFTER:
        changed |= send_daily_system_summary(state, notifier, current, failures=failures)
    changed |= record_notification_counts(
        state,
        target_date,
        success_count=notifier.success_count,
        failure_count=notifier.failure_count,
    )

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
    force_refresh: bool = False,
    notifier: SlackNotifier | None = None,
) -> bool:
    reference_date = as_of or today_jst()
    changed = False
    records = fetch_ipos(force=True) if force_refresh else fetch_ipos()
    for item in records:
        code = item.get("code")
        listing_date = item.get("listing_date")
        if not code:
            continue
        if listing_date and as_date(listing_date) < reference_date:
            continue
        canonical_id = f"ipo-{code}-{listing_date or 'pending'}"
        existing = find_event_by_id(state, canonical_id) or find_latest_event_by_code(state, "ipo", code)
        event_id = existing.get("id") if existing else canonical_id
        previous_listing_date = existing.get("detail", {}).get("listing_date") if existing else None
        master_item = lookup_master(master, code, fallback_name=item.get("name", ""))
        market = master_item["market"] if master_item["market"] != "取得失敗" else item.get("market") or "取得失敗"
        detail = deepcopy(existing.get("detail", {})) if existing else {}
        detail["listing_date"] = listing_date
        event = {
            "id": event_id,
            "type": "ipo",
            "code": code,
            "name": master_item["name"] or item.get("name", ""),
            "market": market,
            "margin": lookup_margin(margin, code),
            "announced_at": existing.get("announced_at") if existing else now_jst().isoformat(),
            "detail": detail,
            "schedule": [],
            "pdf_url": item.get("source_url"),
        }
        transition = eligibility_transition(event)
        if event.get("eligibility", {}).get("status") == ELIGIBLE and listing_date:
            event["schedule"] = build_ipo_schedule(
                listing_date, old_schedule=existing.get("schedule") if existing else None
            )
            if transition == "confirmed" and notifier:
                notifier.send(
                    "ipo",
                    f"[対象確定] {code} {event['name']}({event['market']})\n上場日: {listing_date}",
                    header="IPO 対象確定",
                    pdf_url=event.get("pdf_url"),
                )
                mark_transition_notified(event, transition)
            elif transition == "new":
                mark_transition_notified(event, transition)
        elif transition == "pending" and notifier:
            notifier.send(
                "ipo",
                f"[IPO判定待ち] {code} {event['name']}({event['market']})\n理由: "
                + " / ".join(event["eligibility"]["reasons"]),
                header="IPO判定待ち",
                pdf_url=event.get("pdf_url"),
            )
            mark_transition_notified(event, transition)
        if (
            notifier
            and existing
            and previous_listing_date
            and previous_listing_date != listing_date
            and event.get("eligibility", {}).get("status") == ELIGIBLE
        ):
            notifier.send(
                "ipo",
                f"[上場日変更] {code} {event['name']}: {previous_listing_date} → {listing_date}",
                header="IPO 上場日変更",
                pdf_url=event.get("pdf_url"),
            )
        _, did_change = upsert_event(state, event)
        changed |= did_change

    return changed


def sync_bunbai_events(
    state: dict[str, Any],
    master: dict[str, Any],
    margin: dict[str, str],
    *,
    as_of: date | None = None,
    force_refresh: bool = False,
    notifier: SlackNotifier | None = None,
) -> bool:
    reference_date = as_of or today_jst()
    changed = False
    records = fetch_bunbai(force=True) if force_refresh else fetch_bunbai()
    for item in records:
        code = item.get("code")
        execution_date = item.get("execution_date")
        if not code or not execution_date:
            continue
        if add_business_days(execution_date, 5) < reference_date:
            continue
        canonical_id = f"bunbai-{code}-{execution_date}"
        existing = find_event_by_id(state, canonical_id) or find_matching_bunbai_event(
            state, code, execution_date
        )
        previous_execution_date = existing.get("detail", {}).get("execution_date") if existing else None
        event_id = existing.get("id") if existing else canonical_id
        master_item = lookup_master(
            master,
            code,
            fallback_name=item.get("name", "") or (existing.get("name", "") if existing else ""),
        )
        detail = deepcopy(existing.get("detail", {})) if existing else {}
        legacy_untracked = bool(existing) and not detail.get("notification_tracking_started")
        detail.update({"execution_date": execution_date, "execution_date_confirmed": True})
        detail["notification_tracking_started"] = True
        event = {
            "id": event_id,
            "type": "bunbai",
            "code": code,
            "name": master_item["name"] or item.get("name", ""),
            "market": master_item["market"],
            "margin": lookup_margin(margin, code),
            "announced_at": existing.get("announced_at") if existing else now_jst().isoformat(),
            "detail": detail,
            "schedule": [],
            "pdf_url": existing.get("pdf_url") if existing and existing.get("pdf_url") else item.get("source_url"),
        }
        if existing:
            for key in ["latest_pdf_url", "source_title", "related_disclosures"]:
                if key in existing:
                    event[key] = existing[key]
            if item.get("source_url") and item.get("source_url") != event.get("pdf_url"):
                event["jpx_source_url"] = item["source_url"]
        transition = eligibility_transition(event)
        if event.get("eligibility", {}).get("status") == ELIGIBLE:
            event["schedule"] = build_bunbai_schedule(
                execution_date, old_schedule=existing.get("schedule") if existing else None
            )
        if transition == "new" and legacy_untracked:
            # Existing state predates transition flags. Mark it without replaying an old announcement.
            mark_transition_notified(event, transition)
        elif notifier and transition:
            if transition == "pending":
                label = "立会外分売 判定待ち"
            elif transition == "confirmed":
                label = "立会外分売 対象確定"
            else:
                label = "立会外分売発表"
            notifier.send(
                "bunbai",
                _format_bunbai_sync_message(event, label),
                header=label,
                pdf_url=event.get("latest_pdf_url") or event.get("pdf_url"),
            )
            mark_transition_notified(event, transition)
        elif (
            notifier
            and existing
            and previous_execution_date
            and previous_execution_date != execution_date
            and event.get("eligibility", {}).get("status") == ELIGIBLE
        ):
            label = "実施日変更"
            notifier.send(
                "bunbai",
                _format_bunbai_sync_message(event, label)
                + f"\n変更前: {previous_execution_date}",
                header="立会外分売 実施日変更",
                pdf_url=event.get("latest_pdf_url") or event.get("pdf_url"),
            )
        _, did_change = upsert_event(state, event)
        changed |= did_change
    return changed


def find_event_by_id(state: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    for event in state.get("events", []):
        if event.get("id") == event_id:
            return event
    return None


def find_latest_event_by_code(
    state: dict[str, Any], event_type: str, code: str
) -> dict[str, Any] | None:
    candidates = [
        event
        for event in state.get("events", [])
        if event.get("type") == event_type and event.get("code") == code
    ]
    return max(candidates, key=lambda event: event.get("announced_at", ""), default=None)


def find_pending_bunbai_event(state: dict[str, Any], code: str) -> dict[str, Any] | None:
    candidates = [
        event
        for event in state.get("events", [])
        if event.get("type") == "bunbai"
        and event.get("code") == code
        and not event.get("detail", {}).get("execution_date_confirmed")
    ]
    return max(candidates, key=lambda event: event.get("announced_at", ""), default=None)


def find_matching_bunbai_event(
    state: dict[str, Any], code: str, execution_date: str
) -> dict[str, Any] | None:
    same_date = [
        event
        for event in state.get("events", [])
        if event.get("type") == "bunbai"
        and event.get("code") == code
        and event.get("detail", {}).get("execution_date") == execution_date
    ]
    if same_date:
        return min(same_date, key=lambda event: event.get("announced_at", ""))
    pending = find_pending_bunbai_event(state, code)
    if pending:
        return pending
    try:
        target = as_date(execution_date)
        nearby = [
            event
            for event in state.get("events", [])
            if event.get("type") == "bunbai"
            and event.get("code") == code
            and event.get("detail", {}).get("execution_date")
            and abs((as_date(event["detail"]["execution_date"]) - target).days) <= 45
        ]
    except ValueError:
        return None
    return max(nearby, key=lambda event: event.get("announced_at", ""), default=None)


def notify_system_safely(notifier: SlackNotifier, text: str) -> None:
    try:
        notifier.system(text)
    except Exception as exc:  # Avoid masking the source failure or exposing webhook URLs.
        print(f"System alert failed: {type(exc).__name__}")


def send_due_notifications(
    state: dict[str, Any], notifier: SlackNotifier, current: datetime, *, dry_run: bool
) -> int:
    sent_count = 0
    for due in due_notifications(state, current):
        try:
            notifier.send(
                due.event.get("type", "system"),
                due.text,
                pdf_url=due.event.get("latest_pdf_url") or due.event.get("pdf_url"),
            )
        except Exception as exc:
            notify_system_safely(
                notifier,
                f"予定通知送信失敗: {due.event.get('type')}:{due.event.get('code')} "
                f"{due.schedule_item.get('label')} {type(exc).__name__}",
            )
            continue
        due.schedule_item["sent"] = True
        due.schedule_item["sent_at"] = current.isoformat()
        sent_count += 1
        if not dry_run:
            save_state(state)
    return sent_count


def send_daily_system_summary(
    state: dict[str, Any],
    notifier: SlackNotifier,
    current: datetime,
    *,
    failures: list[str],
) -> bool:
    day = current.date().isoformat()
    summary = state.setdefault("daily_system_summary", {})
    if summary.get("last_sent_date") == day:
        return False
    pending: dict[str, list[str]] = {}
    overdue: list[str] = []
    unresolved: dict[str, set[str]] = {}
    po_missing: list[str] = []
    split_missing: list[str] = []
    for event in state.get("events", []):
        event_type = str(event.get("type") or "event")
        code = str(event.get("code") or "不明")
        if event.get("detail", {}).get("canceled") or event.get("eligibility", {}).get("status") == "excluded":
            continue
        if event.get("eligibility", {}).get("status") == PENDING:
            pending.setdefault(event_type, []).append(code)
            unresolved.setdefault(event_type, set()).add(code)
        if any(item.get("overdue_unresolved") for item in event.get("schedule", [])):
            overdue.append(f"{event_type}:{code}")
            unresolved.setdefault(event_type, set()).add(code)
        detail = event.get("detail", {})
        if event_type == "po":
            missing_fields: list[str] = []
            if detail.get("reference_close_yen") is None and detail.get("confirmed_size_yen") is None:
                missing_fields.append("株価")
            if detail.get("total_offered_shares") is None:
                missing_fields.append("株数")
            if not detail.get("pricing_date"):
                missing_fields.append("価格決定日")
            if missing_fields:
                po_missing.append(f"{code}({','.join(missing_fields)})")
                unresolved.setdefault(event_type, set()).add(code)
        elif event_type == "split":
            missing_fields = []
            if not detail.get("record_date"):
                missing_fields.append("基準日")
            if not detail.get("rights_final_date") or not detail.get("rights_final_confirmed"):
                missing_fields.append("権利付最終日")
            if missing_fields:
                split_missing.append(f"{code}({','.join(missing_fields)})")
                unresolved.setdefault(event_type, set()).add(code)
    previous_stats = state.get("notification_stats", {}).get(day, {})
    success_count = int(previous_stats.get("success", 0)) + notifier.success_count
    failure_count = int(previous_stats.get("failure", 0)) + notifier.failure_count
    lines = [
        f"[日次システム集約] {day}",
        f"当日の通知成功: {success_count}件",
        f"当日の通知失敗: {failure_count}件",
        f"当日の処理失敗: {len(failures)}件",
    ]
    lines.append(
        "イベント別未解決: "
        + (
            " / ".join(f"{key} {len(values)}件" for key, values in sorted(unresolved.items()))
            if unresolved
            else "0件"
        )
    )
    if pending:
        lines.append("判定待ち: " + " / ".join(f"{key} {len(values)}件 ({', '.join(values[:10])})" for key, values in sorted(pending.items())))
    else:
        lines.append("判定待ち: 0件")
    lines.append("PO未取得: " + (" / ".join(po_missing[:20]) if po_missing else "0件"))
    lines.append("株式分割未確定: " + (" / ".join(split_missing[:20]) if split_missing else "0件"))
    lines.append("8日以上の未送信予定: " + (", ".join(overdue[:20]) if overdue else "0件"))
    health = state.get("source_health", {})
    if health:
        lines.append(
            "データソース最終成功: "
            + " / ".join(
                f"{name}={data.get('last_success_at') or data.get('last_success_date', '不明')}"
                for name, data in sorted(health.items())
            )
        )
    notifier.system("\n".join(lines))
    summary["last_sent_date"] = day
    summary["last_sent_at"] = current.isoformat()
    return True


def _format_bunbai_sync_message(event: dict[str, Any], label: str) -> str:
    detail = event.get("detail", {})
    text = (
        f"[{label}] {event.get('code')} {event.get('name')}"
        f"({event.get('market')} / {event.get('margin')})\n"
        f"分売実施日: {detail.get('execution_date') or '要確認'}"
    )
    reasons = event.get("eligibility", {}).get("reasons") or []
    return text + ("\n理由: " + " / ".join(reasons) if reasons else "")


def _resolve_now(now_value: str | None, date_value: str | None) -> datetime:
    if now_value:
        parsed = datetime.fromisoformat(now_value)
        return parsed.astimezone(now_jst().tzinfo) if parsed.tzinfo else parsed.replace(tzinfo=now_jst().tzinfo)
    if date_value:
        return datetime.combine(date.fromisoformat(date_value), time(7, 30), tzinfo=now_jst().tzinfo)
    return now_jst()


def report_cache_refresh(
    notifier: SlackNotifier,
    label: str,
    cache_name: str,
    previous_fetched_at: Any,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    current_fetched_at = cache_fetched_at(cache_name)
    if current_fetched_at is not None and current_fetched_at != previous_fetched_at:
        return
    timestamp = previous_fetched_at.isoformat() if previous_fetched_at is not None else "不明"
    notify_system_safely(notifier, f"{label}の日次更新に失敗し、既存キャッシュを使用しました（取得日時: {timestamp}）")


def record_cached_source_success(state: dict[str, Any], source: str, cache_name: str) -> bool:
    fetched_at = cache_fetched_at(cache_name)
    return record_source_success(state, source, fetched_at) if fetched_at is not None else False


if __name__ == "__main__":
    raise SystemExit(main())
