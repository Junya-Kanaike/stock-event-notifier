from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta
import os
from typing import Any

from src.collectors.jpx_margin import CACHE_NAME as MARGIN_CACHE_NAME, fetch_margin, lookup_margin
from src.collectors.jpx_master import CACHE_NAME as MASTER_CACHE_NAME, fetch_master, lookup_master
from src.collectors.jpx_ex_rights import CACHE_NAME as EX_RIGHTS_CACHE_NAME, fetch_ex_rights
from src.collectors.traders_split import CACHE_NAME as TRADERS_SPLIT_CACHE_NAME, fetch_traders_splits
from src.collectors.tdnet import Disclosure, classify_title, contains_buyback, fetch_disclosures, fetch_pdf_text
from src.collectors.yahoo_price import fetch_close_on_or_before, reference_close_date
from src.collectors.utils import cache_fetched_at
from src.core.bizday import JST, is_business_day, prev_business_day, today_jst
from src.core.dateparse import clean_text, find_dates
from src.core.eligibility import ELIGIBLE, EXCLUDED, PENDING, apply_eligibility
from src.core.po import (
    apply_reference_close,
    format_po_message,
    merge_po_details,
    refresh_calculated_po_size,
)
from src.core.reconcile import reconcile_event_state
from src.core.scheduler import build_bunbai_schedule, build_po_schedule, build_split_schedule
from src.core.split import apply_jpx_ex_right, apply_traders_split
from src.core.store import (
    add_notified_id,
    clear_disclosure_failure,
    find_events,
    has_notified,
    load_state,
    record_disclosure_failure,
    record_notification_counts,
    record_source_result,
    record_source_success,
    save_state,
    trim_notified_ids,
    upsert_event,
)
from src.notifiers.slack import SlackNotifier
from src.core.transitions import eligibility_transition, mark_transition_notified
from src.parsers.po_pdf import parse_po_details
from src.parsers.bunbai_pdf import parse_bunbai_details
from src.parsers.split_pdf import parse_split_details


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="JST date to poll, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    target_date = date.fromisoformat(args.date) if args.date else today_jst()
    if not is_business_day(target_date):
        print(f"{target_date.isoformat()} is not a business day; skip TDnet polling.")
        return 0
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

    try:
        master = fetch_master()
        changed |= record_cached_source_success(state, "jpx_master", MASTER_CACHE_NAME)
    except Exception as exc:
        master = {}
        notify_system_safely(notifier, f"JPX銘柄マスター取得失敗: {exc}")

    try:
        margin = fetch_margin()
        changed |= record_cached_source_success(state, "jpx_margin", MARGIN_CACHE_NAME)
    except Exception as exc:
        margin = {}
        notify_system_safely(notifier, f"JPX信用区分取得失敗: {exc}")

    try:
        ex_rights = fetch_ex_rights()
        changed |= record_cached_source_success(state, "jpx_ex_rights", EX_RIGHTS_CACHE_NAME)
    except Exception as exc:
        ex_rights = {}
        notify_system_safely(notifier, f"JPX権利落情報取得失敗: {exc}")

    try:
        traders_splits = fetch_traders_splits()
        changed |= record_cached_source_success(state, "traders_split", TRADERS_SPLIT_CACHE_NAME)
    except Exception as exc:
        traders_splits = {}
        notify_system_safely(notifier, f"トレーダーズ・ウェブ株式分割取得失敗: {exc}")

    changed |= refresh_event_reference_data(state, master, margin)
    changed |= recover_po_calculations(state, notifier, now=datetime.now(JST))
    changed |= recover_split_events(state, notifier)
    changed |= reconcile_split_traders(state, traders_splits, notifier, as_of=target_date)
    changed |= reconcile_split_ex_rights(state, ex_rights, notifier)
    changed |= refresh_po_reference_prices(state, notifier, now=datetime.now(JST))
    changed |= notify_resolved_reference_transitions(state, notifier)
    changed |= notify_unresolved_split_events(state, notifier)
    if changed and not args.dry_run:
        save_state(state)

    target_dates = poll_target_dates(target_date, explicit_date=bool(args.date))
    disclosures, source_failures = fetch_poll_disclosures(target_dates, notifier)
    changed |= recover_missing_event_markets(state, disclosures)

    if len(source_failures) < len(target_dates):
        health_changed, should_alert = record_source_result(state, "tdnet", target_date, len(disclosures))
        changed |= health_changed
        changed |= record_source_success(state, "tdnet", datetime.now(JST))
        if should_alert:
            notify_system_safely(notifier, "TDnet取得件数が3営業日以上連続で0件です。取得元の仕様変更を確認してください")
        if health_changed and not args.dry_run:
            save_state(state)

    changed |= process_disclosure_batch(
        disclosures,
        state,
        notifier,
        master,
        margin,
        dry_run=args.dry_run,
    )
    changed |= reconcile_split_traders(state, traders_splits, notifier, as_of=target_date)

    changed |= trim_notified_ids(state)
    changed |= record_notification_counts(
        state,
        target_date,
        success_count=notifier.success_count,
        failure_count=notifier.failure_count,
    )
    if changed and not args.dry_run:
        save_state(state)
    if source_failures:
        raise RuntimeError("; ".join(source_failures))
    return 0


def poll_target_dates(target_date: date, *, explicit_date: bool) -> list[date]:
    if explicit_date:
        return [target_date]
    return [prev_business_day(target_date), target_date]


def fetch_poll_disclosures(
    target_dates: list[date], notifier: SlackNotifier
) -> tuple[list[Disclosure], list[str]]:
    by_id: dict[str, Disclosure] = {}
    failures: list[str] = []
    for target_date in target_dates:
        try:
            for disclosure in fetch_disclosures(target_date):
                by_id[disclosure.id] = disclosure
        except Exception as exc:
            message = f"TDnet取得失敗 ({target_date.isoformat()}): {exc}"
            failures.append(message)
            notify_system_safely(notifier, message)
    return sorted(by_id.values(), key=lambda item: (item.announced_at, item.id)), failures


def process_disclosure_batch(
    disclosures: list[Disclosure],
    state: dict[str, Any],
    notifier: SlackNotifier,
    master: dict[str, Any],
    margin: dict[str, str],
    *,
    dry_run: bool,
) -> bool:
    changed = False
    known_buybacks: set[tuple[str, str]] = {
        (item.code, item.announced_at.date().isoformat())
        for item in disclosures
        if "buyback" in classify_title(item.title)
    }
    for disclosure in disclosures:
        if has_notified(state, disclosure.id):
            if "buyback" in classify_title(disclosure.title):
                known_buybacks.add((disclosure.code, disclosure.announced_at.date().isoformat()))
            continue
        classes = classify_title(disclosure.title)
        if not classes:
            continue

        try:
            item_changed = process_disclosure(
                disclosure,
                classes,
                state,
                notifier,
                master,
                margin,
                known_buybacks,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            count, item_changed = record_disclosure_failure(
                state,
                disclosure.id,
                code=disclosure.code,
                title=disclosure.title,
                error=error,
            )
            if count >= 5:
                item_changed |= add_notified_id(state, disclosure.id)
                message = (
                    f"TDnet処理を5回失敗したため打ち切りました: "
                    f"{disclosure.code} {disclosure.title}: {error}"
                )
            else:
                message = (
                    f"TDnet処理失敗 ({count}/5、次回再試行): "
                    f"{disclosure.code} {disclosure.title}: {error}"
                )
            notify_system_safely(notifier, message)
            changed |= item_changed
            if item_changed and not dry_run:
                save_state(state)
            continue

        item_changed |= clear_disclosure_failure(state, disclosure.id)
        item_changed |= add_notified_id(state, disclosure.id)
        changed |= item_changed
        if item_changed and not dry_run:
            save_state(state)
    return changed


def process_disclosure(
    disclosure: Disclosure,
    classes: set[str],
    state: dict[str, Any],
    notifier: SlackNotifier,
    master: dict[str, Any],
    margin: dict[str, str],
    known_buybacks: set[tuple[str, str]],
) -> bool:
    """Process every independent event class carried by one disclosure."""
    changed = False
    if "buyback" in classes:
        changed |= handle_buyback(disclosure, state, notifier)
        known_buybacks.add((disclosure.code, disclosure.announced_at.date().isoformat()))

    if "po_correction" in classes:
        changed |= handle_po_correction(disclosure, state, notifier, master, margin)
    elif "po_pricing" in classes:
        changed |= handle_po_pricing(disclosure, state, notifier, master, margin)
    elif "po" in classes:
        changed |= handle_po(disclosure, state, notifier, master, margin)

    if "bunbai" in classes:
        changed |= handle_bunbai(disclosure, state, notifier, master, margin)
    if "cb" in classes:
        changed |= handle_cb(disclosure, state, notifier, master, margin, known_buybacks)
    if "split" in classes:
        changed |= handle_split(disclosure, state, notifier, master, margin)
    return changed


def base_event(disclosure: Disclosure, event_type: str, master: dict[str, Any], margin: dict[str, str]) -> dict[str, Any]:
    master_item = lookup_master(master, disclosure.code, fallback_name=disclosure.name)
    market = master_item["market"]
    if market == "取得失敗" and disclosure.market:
        market = disclosure.market
    return {
        "id": f"{event_type}-{disclosure.code}-{disclosure.announced_at.date().isoformat()}",
        "type": event_type,
        "code": disclosure.code,
        "name": master_item["name"] or disclosure.name,
        "market": market,
        "margin": lookup_margin(margin, disclosure.code),
        "announced_at": disclosure.announced_at.astimezone(JST).isoformat(),
        "detail": {},
        "schedule": [],
        "pdf_url": disclosure.pdf_url,
        "latest_pdf_url": disclosure.pdf_url,
        "source_title": disclosure.title,
        "related_disclosures": [disclosure_reference(disclosure, "source")],
    }


def refresh_event_reference_data(
    state: dict[str, Any], master: dict[str, Any], margin: dict[str, str]
) -> bool:
    changed = False
    for event in state.get("events", []):
        before = deepcopy(event)
        code = str(event.get("code") or "")
        if code and master:
            item = lookup_master(master, code, fallback_name=event.get("name", ""))
            if item.get("name"):
                event["name"] = item["name"]
            if item.get("market") != "取得失敗":
                event["market"] = item["market"]
        if code and margin:
            event["margin"] = lookup_margin(margin, code)
        apply_eligibility(event)
        changed |= event != before
    return changed


def apply_po_reference_price(
    event: dict[str, Any], notifier: SlackNotifier, *, now: datetime
) -> bool:
    detail = event.setdefault("detail", {})
    try:
        announced_at = datetime.fromisoformat(str(event.get("announced_at")))
    except (TypeError, ValueError):
        add_parse_warning(detail, "⚠️ 発表日時を解釈できません")
        refresh_calculated_po_size(detail)
        return False
    target, reference_kind = reference_close_date(announced_at, as_of=now)
    if detail.get("reference_close_date") == target.isoformat() and detail.get("reference_close_yen") is not None:
        refresh_calculated_po_size(detail)
        return False
    try:
        price = fetch_close_on_or_before(str(event.get("code") or ""), target)
    except Exception as exc:
        add_parse_warning(detail, f"⚠️ Yahoo終値取得失敗 ({type(exc).__name__})")
        refresh_calculated_po_size(detail)
        failure_key = f"{target.isoformat()}:{type(exc).__name__}"
        if detail.get("reference_close_failure_key") != failure_key:
            notify_system_safely(
                notifier,
                f"Yahoo株価取得失敗: {event.get('code')} {target.isoformat()} {type(exc).__name__}: {exc}",
            )
            detail["reference_close_failure_key"] = failure_key
        return False
    apply_reference_close(detail, price, reference_kind)
    detail.pop("reference_close_failure_key", None)
    return True


def refresh_po_reference_prices(
    state: dict[str, Any], notifier: SlackNotifier, *, now: datetime
) -> bool:
    changed = False
    yahoo_succeeded = False
    for event in find_events(state, event_type="po"):
        detail = event.setdefault("detail", {})
        if detail.get("size_status") == "confirmed" and detail.get("confirmed_size_yen") is not None:
            continue
        before = deepcopy(event)
        price_changed = apply_po_reference_price(event, notifier, now=now)
        yahoo_succeeded |= price_changed
        apply_eligibility(event)
        if event.get("eligibility", {}).get("status") == ELIGIBLE and detail.get("pricing_date"):
            event["schedule"] = build_po_schedule(
                detail["pricing_date"], detail.get("settlement_date"), old_schedule=event.get("schedule", [])
            )
        transition = eligibility_transition(event)
        if detail.get("notification_tracking_started"):
            notify_po_state(notifier, event, transition)
        changed |= event != before
    if yahoo_succeeded:
        changed |= record_source_success(state, "yahoo_price", now)
    return changed


def recover_po_calculations(
    state: dict[str, Any], notifier: SlackNotifier, *, now: datetime
) -> bool:
    changed = False
    for event in find_events(
        state,
        event_type="po",
        predicate=lambda item: item.get("detail", {}).get("parser_version") != 2
        or not item.get("detail", {}).get("total_offered_shares")
        or item.get("detail", {}).get("effective_size_yen") is None,
    ):
        before = deepcopy(event)
        detail = event.setdefault("detail", {})
        sources: list[dict[str, Any]] = [
            {
                "pdf_url": event.get("pdf_url"),
                "title": event.get("source_title") or "PO発表",
                "announced_at": event.get("announced_at"),
            }
        ]
        sources.extend(event.get("related_disclosures") or [])
        by_url = {
            str(source.get("pdf_url")): source
            for source in sources
            if source.get("pdf_url")
        }
        latest = event.get("latest_pdf_url")
        if latest and str(latest) not in by_url:
            by_url[str(latest)] = {
                "pdf_url": latest,
                "title": event.get("source_title") or "PO更新",
                "announced_at": event.get("announced_at"),
            }
        try:
            for source in by_url.values():
                disclosure_date = date.fromisoformat(str(source.get("announced_at", ""))[:10])
                text = fetch_pdf_text(str(source["pdf_url"]))
                parsed = parse_po_details(str(source.get("title") or "PO発表"), text, disclosure_date)
                detail = merge_po_details(detail, parsed)
                if parsed.get("source_stage") == "pricing":
                    pricing_date = disclosure_date.isoformat()
                    detail["pricing_date"] = pricing_date
                    detail["pricing_date_end"] = pricing_date
                    detail["pricing_date_confirmed"] = True
                    detail["pricing_date_status"] = "confirmed"
            event["detail"] = detail
            if apply_po_reference_price(event, notifier, now=now):
                changed |= record_source_success(state, "yahoo_price", now)
            refresh_calculated_po_size(detail)
            apply_eligibility(event)
            if event.get("eligibility", {}).get("status") == ELIGIBLE and detail.get("pricing_date"):
                event["schedule"] = build_po_schedule(
                    detail["pricing_date"], detail.get("settlement_date"), old_schedule=event.get("schedule", [])
                )
            detail.pop("calculation_recovery_last_error", None)
            detail.pop("calculation_recovery_alerted", None)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            detail["calculation_recovery_last_error"] = error
            if not detail.get("calculation_recovery_alerted"):
                notify_system_safely(notifier, f"PO吸収規模の再計算失敗: {event.get('code')} {error}")
                detail["calculation_recovery_alerted"] = True
        changed |= event != before
    return changed


def enrich_event_markets(state: dict[str, Any], disclosures: list[Disclosure]) -> bool:
    by_id = {item.id: item for item in disclosures if item.market}
    changed = False
    for event in state.get("events", []):
        if event.get("market") != "取得失敗":
            continue
        reference_ids = {item.get("id") for item in event.get("related_disclosures", [])}
        source = next((by_id[item_id] for item_id in reference_ids if item_id in by_id), None)
        if source and source.market:
            event["market"] = source.market
            changed = True
    return changed


def recover_missing_event_markets(
    state: dict[str, Any], known_disclosures: list[Disclosure]
) -> bool:
    changed = enrich_event_markets(state, known_disclosures)
    known_ids = {item.id for item in known_disclosures}
    dates: set[date] = set()
    for event in state.get("events", []):
        if event.get("market") != "取得失敗":
            continue
        references = event.get("related_disclosures", [])
        for reference in references:
            if reference.get("id") in known_ids:
                continue
            try:
                dates.add(date.fromisoformat(str(reference.get("announced_at", ""))[:10]))
            except ValueError:
                continue

    recovered: list[Disclosure] = []
    for target_date in sorted(dates)[:10]:
        try:
            recovered.extend(fetch_disclosures(target_date))
        except Exception:
            continue
    if recovered:
        changed |= enrich_event_markets(state, recovered)
    return changed


def fetch_disclosure_text_safely(
    disclosure: Disclosure, notifier: SlackNotifier
) -> tuple[str, str | None]:
    if not disclosure.pdf_url:
        warning = "⚠️ PDF URLなし"
        notify_system_safely(notifier, f"TDnet PDF取得失敗: {disclosure.code} {warning}")
        return "", warning
    try:
        text = fetch_pdf_text(disclosure.pdf_url)
    except Exception as exc:
        warning = f"⚠️ PDF取得失敗 ({type(exc).__name__})"
        notify_system_safely(notifier, f"TDnet PDF取得失敗: {disclosure.code} {warning}")
        return "", warning
    if not text.strip():
        warning = "⚠️ PDF本文を抽出できません"
        notify_system_safely(notifier, f"TDnet PDF本文抽出失敗: {disclosure.code}")
        return "", warning
    return text, None


def add_parse_warning(detail: dict[str, Any], warning: str | None) -> None:
    if not warning:
        return
    warnings = detail.setdefault("parse_warnings", [])
    if warning not in warnings:
        warnings.append(warning)


def handle_po(disclosure: Disclosure, state: dict[str, Any], notifier: SlackNotifier, master: dict[str, Any], margin: dict[str, str]) -> bool:
    if is_cancellation_title(disclosure.title):
        return cancel_matching_event(state, notifier, "po", disclosure, "PO", notify=False)
    text, pdf_warning = fetch_disclosure_text_safely(disclosure, notifier)
    event = base_event(disclosure, "po", master, margin)
    event["detail"] = parse_po_details(disclosure.title, text, disclosure.announced_at.date())
    event["detail"]["notification_tracking_started"] = True
    add_parse_warning(event["detail"], pdf_warning)
    reference_now = datetime.now(JST)
    if apply_po_reference_price(event, notifier, now=reference_now):
        record_source_success(state, "yahoo_price", reference_now)
    transition = eligibility_transition(event)
    if event.get("eligibility", {}).get("status") == ELIGIBLE and event["detail"].get("pricing_date"):
        event["schedule"] = build_po_schedule(event["detail"]["pricing_date"], event["detail"].get("settlement_date"))
    elif not event["detail"].get("pricing_date"):
        notify_system_safely(notifier, f"PO価格決定日の抽出失敗: {disclosure.code} {disclosure.title}")
    notify_po_state(notifier, event, transition)
    upsert_event(state, event)
    return True


def handle_po_pricing(
    disclosure: Disclosure,
    state: dict[str, Any],
    notifier: SlackNotifier,
    master: dict[str, Any],
    margin: dict[str, str],
) -> bool:
    text, pdf_warning = fetch_disclosure_text_safely(disclosure, notifier)
    parsed_detail = parse_po_details(disclosure.title, text, disclosure.announced_at.date())
    add_parse_warning(parsed_detail, pdf_warning)
    candidates = find_events(
        state,
        event_type="po",
        code=disclosure.code,
        predicate=lambda event: not event.get("detail", {}).get("pricing_date_confirmed"),
    )
    if not candidates:
        event = base_event(disclosure, "po", master, margin)
        detail = parsed_detail
        detail["notification_tracking_started"] = True
        pricing_date = disclosure.announced_at.date().isoformat()
        detail["pricing_date"] = pricing_date
        detail["pricing_date_end"] = pricing_date
        detail["pricing_date_confirmed"] = True
        detail["pricing_date_status"] = "confirmed"
        add_parse_warning(detail, "当初発表を取得できず価格決定資料から復元")
        refresh_calculated_po_size(detail)
        event["detail"] = detail
        transition = eligibility_transition(event)
        if event.get("eligibility", {}).get("status") == ELIGIBLE:
            event["schedule"] = build_po_schedule(pricing_date, detail.get("settlement_date"))
        notify_po_state(notifier, event, transition)
        upsert_event(state, event)
        return True
    event = sorted(candidates, key=lambda item: item.get("announced_at", ""), reverse=True)[0]
    updated = deepcopy(event)
    old_schedule = updated.get("schedule", [])
    detail = merge_po_details(updated.get("detail", {}), parsed_detail)
    detail["notification_tracking_started"] = True
    pricing_date = disclosure.announced_at.date().isoformat()
    detail["pricing_date"] = pricing_date
    detail["pricing_date_end"] = pricing_date
    detail["pricing_date_confirmed"] = True
    detail["pricing_date_status"] = "confirmed"
    refresh_calculated_po_size(detail)
    updated["detail"] = detail
    updated["latest_pdf_url"] = disclosure.pdf_url
    append_related_disclosure(updated, disclosure, "pricing")
    transition = eligibility_transition(updated)
    if updated.get("eligibility", {}).get("status") == ELIGIBLE:
        updated["schedule"] = build_po_schedule(pricing_date, detail.get("settlement_date"), old_schedule=old_schedule)
    else:
        updated["schedule"] = []
    notify_po_state(notifier, updated, transition)
    upsert_event(state, updated)
    return True


def handle_po_correction(
    disclosure: Disclosure,
    state: dict[str, Any],
    notifier: SlackNotifier,
    master: dict[str, Any],
    margin: dict[str, str],
) -> bool:
    if is_cancellation_title(disclosure.title):
        return cancel_matching_event(state, notifier, "po", disclosure, "PO")
    text, pdf_warning = fetch_disclosure_text_safely(disclosure, notifier)
    parsed_detail = parse_po_details(disclosure.title, text, disclosure.announced_at.date())
    add_parse_warning(parsed_detail, pdf_warning)
    candidates = find_events(state, event_type="po", code=disclosure.code)
    if candidates:
        original = sorted(candidates, key=lambda item: item.get("announced_at", ""), reverse=True)[0]
        updated = deepcopy(original)
        updated["detail"] = merge_po_details(updated.get("detail", {}), parsed_detail)
        updated["latest_pdf_url"] = disclosure.pdf_url
        append_related_disclosure(updated, disclosure, "correction")
        if updated["detail"].get("pricing_date"):
            updated["schedule"] = build_po_schedule(
                updated["detail"]["pricing_date"],
                updated["detail"].get("settlement_date"),
                old_schedule=updated.get("schedule", []),
            )
    else:
        updated = recover_original_po_event(disclosure, text, master, margin)
        if updated:
            updated["detail"] = merge_po_details(updated.get("detail", {}), parsed_detail)
            updated["detail"].setdefault("recovery_notes", []).append("訂正資料から元開示を自動補完")
            updated["latest_pdf_url"] = disclosure.pdf_url
            append_related_disclosure(updated, disclosure, "correction")
        else:
            updated = base_event(disclosure, "po", master, margin)
            add_parse_warning(parsed_detail, "元のPO発表を状態ストアまたは訂正資料から特定できません")
            refresh_calculated_po_size(parsed_detail)
            updated["detail"] = parsed_detail
        updated["detail"]["notification_tracking_started"] = True
        if updated["detail"].get("pricing_date"):
            updated["schedule"] = build_po_schedule(
                updated["detail"]["pricing_date"], updated["detail"].get("settlement_date")
            )
    refresh_calculated_po_size(updated["detail"])
    transition = eligibility_transition(updated)
    if updated.get("eligibility", {}).get("status") != ELIGIBLE:
        updated["schedule"] = []
    notify_po_state(notifier, updated, transition)
    upsert_event(state, updated)
    return True


def notify_po_state(
    notifier: SlackNotifier,
    event: dict[str, Any],
    transition: str | None,
) -> None:
    if transition == "pending":
        mark_transition_notified(event, transition)
        return
    if transition not in {"new", "confirmed"}:
        return
    label = "PO予定通知"
    notifier.send(
        "po",
        format_po_message(event, label),
        header=label,
        pdf_url=event.get("latest_pdf_url") or event.get("pdf_url"),
    )
    if transition:
        mark_transition_notified(event, transition)


def handle_bunbai(disclosure: Disclosure, state: dict[str, Any], notifier: SlackNotifier, master: dict[str, Any], margin: dict[str, str]) -> bool:
    if is_cancellation_title(disclosure.title):
        return cancel_matching_event(state, notifier, "bunbai", disclosure, "立会外分売")
    text, pdf_warning = fetch_disclosure_text_safely(disclosure, notifier)
    parsed_detail = parse_bunbai_details(text, disclosure.announced_at.date())
    add_parse_warning(parsed_detail, pdf_warning)
    followup = is_bunbai_followup_title(disclosure.title)
    existing = find_bunbai_event_for_update(state, disclosure.code, parsed_detail.get("execution_date"), followup)
    if existing:
        event = deepcopy(existing)
        detail = event.setdefault("detail", {})
        previous_execution_date = detail.get("execution_date")
        for key, value in parsed_detail.items():
            if value is not None:
                detail[key] = value
        detail["execution_date_confirmed"] = bool(detail.get("execution_date_confirmed")) or followup
        event["latest_pdf_url"] = disclosure.pdf_url
        append_related_disclosure(event, disclosure, bunbai_relation(disclosure.title))
    else:
        event = base_event(disclosure, "bunbai", master, margin)
        parsed_detail["execution_date_confirmed"] = followup
        event["detail"] = parsed_detail
        previous_execution_date = None

    event["detail"]["notification_tracking_started"] = True

    transition = eligibility_transition(event)
    if event.get("eligibility", {}).get("status") == ELIGIBLE and event["detail"].get("execution_date"):
        event["schedule"] = build_bunbai_schedule(
            event["detail"]["execution_date"], old_schedule=event.get("schedule", [])
        )
    else:
        event["schedule"] = []
    if not event["detail"].get("execution_date"):
        notify_system_safely(notifier, f"立会外分売実施日の抽出失敗: {disclosure.code} {disclosure.title}")
    label = None
    if transition == "pending":
        label = "立会外分売 判定待ち"
    elif transition == "confirmed":
        label = "立会外分売 対象確定"
    elif transition == "new":
        label = "立会外分売発表"
    elif existing and event.get("eligibility", {}).get("status") == ELIGIBLE:
        label = (
            "実施日変更"
            if previous_execution_date
            and previous_execution_date != event["detail"].get("execution_date")
            else "立会外分売更新"
        )
    if label:
        notifier.send("bunbai", format_bunbai_announcement(event, label), header=label, pdf_url=disclosure.pdf_url)
        if transition:
            mark_transition_notified(event, transition)
    upsert_event(state, event)
    return True


def handle_cb(
    disclosure: Disclosure,
    state: dict[str, Any],
    notifier: SlackNotifier,
    master: dict[str, Any],
    margin: dict[str, str],
    same_day_buybacks: set[tuple[str, str]],
) -> bool:
    text, pdf_warning = fetch_disclosure_text_safely(disclosure, notifier)
    same_day_key = (disclosure.code, disclosure.announced_at.date().isoformat())
    event = base_event(disclosure, "cb", master, margin)
    event["detail"] = {"amount": extract_cb_amount(text), "canceled": False}
    add_parse_warning(event["detail"], pdf_warning)
    if same_day_key in same_day_buybacks or contains_buyback(disclosure.title) or contains_buyback(text):
        event["detail"]["canceled"] = True
        event["detail"]["cancel_reason"] = "自社株買い同時発表を確認"
        event["eligibility"] = {"status": EXCLUDED, "reasons": ["自社株買い同時発表"]}
        upsert_event(state, event)
        return True
    transition = eligibility_transition(event)
    label = "CB判定待ち" if transition == "pending" else "CB対象確定" if transition == "confirmed" else "CB発表" if transition == "new" else None
    if label:
        notifier.send("cb", format_cb_announcement(event, label), header=label, pdf_url=event.get("pdf_url"))
        mark_transition_notified(event, transition)
    upsert_event(state, event)
    return True


def handle_split(
    disclosure: Disclosure,
    state: dict[str, Any],
    notifier: SlackNotifier,
    master: dict[str, Any],
    margin: dict[str, str],
) -> bool:
    if is_cancellation_title(disclosure.title):
        return cancel_matching_event(state, notifier, "split", disclosure, "株式分割")
    text, pdf_warning = fetch_disclosure_text_safely(disclosure, notifier)
    parsed_detail = parse_split_details(text, disclosure.announced_at.date())
    add_parse_warning(parsed_detail, pdf_warning)
    existing = find_split_event_for_update(state, disclosure)
    if existing:
        event = deepcopy(existing)
        detail = event.setdefault("detail", {})
        for key, value in parsed_detail.items():
            if value is not None:
                detail[key] = value
        event["latest_pdf_url"] = disclosure.pdf_url
        append_related_disclosure(event, disclosure, "correction")
    else:
        event = base_event(disclosure, "split", master, margin)
        event["detail"] = parsed_detail
    transition = eligibility_transition(event)
    if event.get("eligibility", {}).get("status") == ELIGIBLE and event["detail"].get("rights_final_date"):
        event["schedule"] = build_split_schedule(
            event["detail"]["rights_final_date"], old_schedule=event.get("schedule", [])
        )
    else:
        event["schedule"] = []
    if transition == "pending":
        event["detail"]["review_notified"] = True
        mark_transition_notified(event, transition)
    elif transition == "confirmed":
        mark_transition_notified(event, transition)
    elif transition == "new":
        # 通常の株式分割発表は通知せず、予定だけ登録する。
        mark_transition_notified(event, transition)
    _, changed = upsert_event(state, event)
    return changed


def recover_split_events(state: dict[str, Any], notifier: SlackNotifier) -> bool:
    """Reparse legacy split events that do not yet have a rights-final date."""
    changed = False
    for event in find_events(
        state,
        event_type="split",
        predicate=lambda item: (
            bool(item.get("detail", {}).get("recovery_needed"))
            or not item.get("detail", {}).get("rights_final_date")
        )
        and item.get("eligibility", {}).get("status") != EXCLUDED,
    ):
        detail = event.setdefault("detail", {})
        try:
            pdf_url = event.get("latest_pdf_url") or event.get("pdf_url")
            if not pdf_url:
                raise ValueError("PDF URLなし")
            text = fetch_pdf_text(pdf_url)
            disclosure_date = date.fromisoformat(str(event.get("announced_at", ""))[:10])
            parsed = parse_split_details(text, disclosure_date)
            if parsed.get("ratio") == "1":
                parsed["ratio"] = None
            for key, value in parsed.items():
                if value is not None:
                    detail[key] = value
            if detail.get("rights_final_date"):
                event["schedule"] = build_split_schedule(
                    detail["rights_final_date"], old_schedule=event.get("schedule", [])
                )
            apply_eligibility(event)
            if not detail.get("ratio") or not detail.get("rights_final_date"):
                raise ValueError("分割比率または権利付最終日を抽出できません")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if detail.get("recovery_last_error") != error:
                detail["recovery_last_error"] = error
                changed = True
            if not detail.get("recovery_alerted"):
                notify_system_safely(notifier, f"株式分割の再抽出失敗: {event.get('code')} {error}")
                detail["recovery_alerted"] = True
                changed = True
            continue

        for key in ["recovery_needed", "recovery_reason", "recovery_last_error", "recovery_alerted"]:
            detail.pop(key, None)
        changed = True
    return changed


def notify_unresolved_split_events(state: dict[str, Any], notifier: SlackNotifier) -> bool:
    """Record unresolved splits without flooding the trading channel."""
    changed = False
    for event in find_events(state, event_type="split"):
        changed |= apply_eligibility(event)
    for event in find_events(
        state,
        event_type="split",
        predicate=lambda item: item.get("eligibility", {}).get("status") == PENDING
        and not item.get("detail", {}).get("review_notified"),
    ):
        event.setdefault("detail", {})["review_notified"] = True
        event["detail"]["pending_notified"] = True
        changed = True
    return changed


def notify_resolved_reference_transitions(state: dict[str, Any], notifier: SlackNotifier) -> bool:
    """Notify only genuine pending-to-eligible transitions after reference refreshes."""
    changed = False
    for event in state.get("events", []):
        detail = event.setdefault("detail", {})
        if (
            event.get("detail", {}).get("canceled")
            or event.get("eligibility", {}).get("status") != ELIGIBLE
            or not detail.get("pending_notified")
            or detail.get("eligible_notified")
        ):
            continue
        before = deepcopy(event)
        try:
            event_type = event.get("type")
            if event_type == "po" and detail.get("pricing_date"):
                event["schedule"] = build_po_schedule(
                    detail["pricing_date"], detail.get("settlement_date"), old_schedule=event.get("schedule", [])
                )
            elif event_type == "bunbai" and detail.get("execution_date"):
                event["schedule"] = build_bunbai_schedule(
                    detail["execution_date"], old_schedule=event.get("schedule", [])
                )
            elif event_type == "split" and detail.get("rights_final_date"):
                event["schedule"] = build_split_schedule(
                    detail["rights_final_date"], old_schedule=event.get("schedule", [])
                )
        except Exception as exc:
            event["schedule"] = []
            notify_system_safely(
                notifier,
                f"予定通知生成不能: {event.get('type')}:{event.get('code')} {type(exc).__name__}: {exc}",
            )
            changed |= event != before
            continue

        event_type = event.get("type")
        if event_type == "po":
            text, header = format_po_message(event, "PO予定通知"), "PO予定通知"
        elif event_type == "bunbai":
            text, header = format_bunbai_announcement(event, "立会外分売 対象確定"), "立会外分売 対象確定"
        elif event_type == "cb":
            text, header = format_cb_announcement(event, "CB対象確定"), "CB対象確定"
        elif event_type == "split":
            mark_transition_notified(event, "confirmed")
            changed |= event != before
            continue
        else:
            continue
        notifier.send(
            str(event_type),
            text,
            header=header,
            pdf_url=event.get("latest_pdf_url") or event.get("pdf_url"),
        )
        mark_transition_notified(event, "confirmed")
        changed |= event != before
    return changed


def reconcile_split_traders(
    state: dict[str, Any],
    records: dict[str, list[dict[str, Any]]],
    notifier: SlackNotifier,
    *,
    as_of: date | None = None,
) -> bool:
    """Resolve split dates/ratios from the user-designated Traders Web source."""
    changed = False
    for event in find_events(state, event_type="split"):
        record = _matching_traders_split(event, records.get(str(event.get("code") or ""), []))
        if not record:
            continue
        before = deepcopy(event)
        detail = event.setdefault("detail", {})
        had_rights_final = bool(detail.get("rights_final_date"))
        apply_traders_split(detail, record)
        apply_eligibility(event)
        if detail.get("rights_date_conflict"):
            event["schedule"] = []
            if not detail.get("traders_conflict_notified"):
                notify_system_safely(
                    notifier,
                    f"株式分割の権利付最終日がトレーダーズ・ウェブと不一致: {event.get('code')} "
                    f"PDF/JPX={detail.get('rights_final_date')} Traders={detail.get('traders_rights_final_date')}",
                )
                detail["traders_conflict_notified"] = True
        elif event.get("eligibility", {}).get("status") == ELIGIBLE:
            event["schedule"] = build_split_schedule(
                detail["rights_final_date"], old_schedule=event.get("schedule", [])
            )
            if not had_rights_final:
                reference_date = as_of or today_jst()
                for item in event["schedule"]:
                    if date.fromisoformat(item["date"]) < reference_date:
                        item["sent"] = True
                        item["suppressed_reason"] = "reference_resolved_after_scheduled_date"
            transition = eligibility_transition(event)
            if transition in {"new", "confirmed"}:
                mark_transition_notified(event, transition)
        changed |= event != before
    return changed


def _matching_traders_split(
    event: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not candidates:
        return None
    detail = event.get("detail", {})
    effective = detail.get("effective_date")
    if effective:
        exact = [record for record in candidates if record.get("effective_date") == effective]
        if exact:
            return exact[-1]
    try:
        announced = date.fromisoformat(str(event.get("announced_at", ""))[:10])
    except ValueError:
        return None
    nearby = []
    for record in candidates:
        try:
            rights_final = date.fromisoformat(str(record.get("rights_final_date")))
        except ValueError:
            continue
        if announced <= rights_final <= announced + timedelta(days=370):
            nearby.append(record)
    return min(nearby, key=lambda record: record["rights_final_date"], default=None)


def reconcile_split_ex_rights(
    state: dict[str, Any], records: dict[str, dict[str, Any]], notifier: SlackNotifier
) -> bool:
    changed = False
    for event in find_events(state, event_type="split"):
        record = records.get(str(event.get("code") or ""))
        if not record or not _jpx_record_matches_event_cycle(event, record):
            continue
        before = deepcopy(event)
        detail = event.setdefault("detail", {})
        apply_jpx_ex_right(detail, record)
        apply_eligibility(event)
        if detail.get("rights_date_conflict"):
            event["schedule"] = []
            if not detail.get("rights_conflict_notified"):
                notify_system_safely(
                    notifier,
                    f"株式分割の権利付最終日がJPX情報と不一致: {event.get('code')} "
                    f"PDF/計算={detail.get('rights_final_date')} JPX={detail.get('jpx_rights_final_date')}",
                )
                detail["rights_conflict_notified"] = True
                detail["pending_notified"] = True
        elif event.get("eligibility", {}).get("status") == ELIGIBLE:
            event["schedule"] = build_split_schedule(
                detail["rights_final_date"], old_schedule=event.get("schedule", [])
            )
            transition = eligibility_transition(event)
            if transition in {"new", "confirmed"}:
                mark_transition_notified(event, transition)
        changed |= event != before
    return changed


def _jpx_record_matches_event_cycle(event: dict[str, Any], record: dict[str, Any]) -> bool:
    """Avoid comparing an archived/older split cycle with a newer JPX row for the same code."""
    detail = event.get("detail", {})
    anchors = [
        (detail.get("record_date"), record.get("record_date")),
        (detail.get("rights_final_date"), record.get("rights_final_date")),
        (detail.get("ex_right_date"), record.get("ex_right_date")),
    ]
    for event_value, record_value in anchors:
        if not event_value or not record_value:
            continue
        try:
            distance = abs((date.fromisoformat(str(event_value)) - date.fromisoformat(str(record_value))).days)
        except ValueError:
            continue
        if distance > 45:
            return False
        return True
    effective = detail.get("effective_date")
    record_date = record.get("record_date")
    if effective and record_date:
        try:
            return abs((date.fromisoformat(str(effective)) - date.fromisoformat(str(record_date))).days) <= 45
        except ValueError:
            pass
    return True


def handle_buyback(disclosure: Disclosure, state: dict[str, Any], notifier: SlackNotifier) -> bool:
    changed = False
    disclosure_day = disclosure.announced_at.date().isoformat()
    for event in find_events(state, event_type="cb", code=disclosure.code):
        if event.get("announced_at", "")[:10] != disclosure_day:
            continue
        detail = event.setdefault("detail", {})
        if detail.get("canceled"):
            continue
        notifier.send(
            "cb",
            f"⚠️ [取消] {event.get('code')} {event.get('name')}: 自社株買い同時発表を確認",
            header="CB取消",
        )
        detail["canceled"] = True
        detail["cancel_reason"] = "自社株買い同時発表を確認"
        changed = True
    return changed


def extract_cb_amount(text: str) -> str | None:
    import re

    normalized = re.sub(r"\s+", "", text or "")
    match = re.search(r"([0-9,]+(?:\.[0-9]+)?億円)", normalized)
    return match.group(1) if match else None


def format_po_announcement(event: dict[str, Any]) -> str:
    return format_po_message(event, "PO予定通知")


def disclosure_reference(disclosure: Disclosure, relation: str) -> dict[str, Any]:
    reference = {
        "id": disclosure.id,
        "relation": relation,
        "title": disclosure.title,
        "announced_at": disclosure.announced_at.astimezone(JST).isoformat(),
        "pdf_url": disclosure.pdf_url,
    }
    if disclosure.market:
        reference["market"] = disclosure.market
    return reference


def append_related_disclosure(event: dict[str, Any], disclosure: Disclosure, relation: str) -> None:
    references = event.setdefault("related_disclosures", [])
    if any(item.get("id") == disclosure.id for item in references):
        return
    references.append(disclosure_reference(disclosure, relation))


def is_bunbai_followup_title(title: str) -> bool:
    normalized = clean_text(title).replace(" ", "")
    return any(marker in normalized for marker in ["分売実施", "分売終了", "分売条件", "訂正", "変更", "延期"])


def bunbai_relation(title: str) -> str:
    normalized = clean_text(title).replace(" ", "")
    if "終了" in normalized:
        return "completion"
    if "実施" in normalized or "条件" in normalized:
        return "execution"
    return "update"


def find_bunbai_event_for_update(
    state: dict[str, Any],
    code: str,
    execution_date: str | None,
    followup: bool,
) -> dict[str, Any] | None:
    candidates = find_events(state, event_type="bunbai", code=code)
    same_date = [
        event for event in candidates if execution_date and event.get("detail", {}).get("execution_date") == execution_date
    ]
    if same_date:
        return min(same_date, key=lambda event: event.get("announced_at", ""))
    if not followup:
        return None
    pending = [event for event in candidates if not event.get("detail", {}).get("execution_date_confirmed")]
    if pending:
        return max(pending, key=lambda event: event.get("announced_at", ""))
    return max(candidates, key=lambda event: event.get("announced_at", ""), default=None)


def find_split_event_for_update(state: dict[str, Any], disclosure: Disclosure) -> dict[str, Any] | None:
    normalized = clean_text(disclosure.title).replace(" ", "")
    if not any(marker in normalized for marker in ["訂正", "変更", "延期"]):
        return None
    candidates: list[dict[str, Any]] = []
    disclosure_day = disclosure.announced_at.date()
    for event in find_events(state, event_type="split", code=disclosure.code):
        try:
            event_day = date.fromisoformat(str(event.get("announced_at", ""))[:10])
        except ValueError:
            continue
        if 0 <= (disclosure_day - event_day).days <= 180:
            candidates.append(event)
    return max(candidates, key=lambda event: event.get("announced_at", ""), default=None)


def is_cancellation_title(title: str) -> bool:
    normalized = clean_text(title).replace(" ", "")
    return any(marker in normalized for marker in ["中止", "撤回"])


def cancel_matching_event(
    state: dict[str, Any],
    notifier: SlackNotifier,
    event_type: str,
    disclosure: Disclosure,
    event_name: str,
    *,
    notify: bool = True,
) -> bool:
    candidates = find_events(state, event_type=event_type, code=disclosure.code)
    if not candidates:
        return True
    event = max(candidates, key=lambda item: item.get("announced_at", ""))
    detail = event.setdefault("detail", {})
    if detail.get("canceled"):
        return False
    detail["canceled"] = True
    detail["cancel_reason"] = disclosure.title
    event["schedule"] = []
    event["latest_pdf_url"] = disclosure.pdf_url
    append_related_disclosure(event, disclosure, "cancellation")
    event["eligibility"] = {"status": EXCLUDED, "reasons": ["イベント中止"]}
    if notify:
        notifier.send(
            event_type,
            f"[中止] {event.get('code')} {event.get('name')} ({event_name})\n{disclosure.title}",
            header=f"{event_name} 中止",
            pdf_url=disclosure.pdf_url,
        )
    return True


def recover_original_po_event(
    correction: Disclosure,
    correction_text: str,
    master: dict[str, Any],
    margin: dict[str, str],
) -> dict[str, Any] | None:
    original_date = original_disclosure_date(correction_text, correction.announced_at.date().year)
    if not original_date:
        return None
    try:
        disclosures = fetch_disclosures(original_date)
    except Exception:
        return None
    candidates = [
        item
        for item in disclosures
        if item.code == correction.code and "po" in classify_title(item.title) and "po_correction" not in classify_title(item.title)
    ]
    if not candidates:
        return None
    original = sorted(candidates, key=lambda item: (item.announced_at, item.id))[-1]
    try:
        original_text = fetch_pdf_text(original.pdf_url)
    except Exception:
        return None
    event = base_event(original, "po", master, margin)
    event["related_disclosures"] = [disclosure_reference(original, "original")]
    event["detail"] = parse_po_details(original.title, original_text, original.announced_at.date())
    if event["detail"].get("pricing_date"):
        event["schedule"] = build_po_schedule(
            event["detail"]["pricing_date"], event["detail"].get("settlement_date")
        )
    return event


def original_disclosure_date(text: str, default_year: int) -> date | None:
    normalized = clean_text(text)
    marker = "に開示いたしました"
    position = normalized.find(marker)
    if position < 0:
        return None
    dates = find_dates(normalized[max(0, position - 60) : position], default_year=default_year)
    return dates[-1] if dates else None


def format_bunbai_announcement(event: dict[str, Any], label: str = "立会外分売発表") -> str:
    detail = event.get("detail", {})
    execution = detail.get("execution_date") or "要確認"
    text = f"[{label}] {event['code']} {event['name']}({event['market']} / {event['margin']})\n分売実施日: {execution}"
    if detail.get("parse_warnings"):
        text += "\n注意: " + " / ".join(detail["parse_warnings"])
    return text


def format_cb_announcement(event: dict[str, Any], label: str = "CB発表") -> str:
    detail = event.get("detail", {})
    amount = detail.get("amount") or "取得失敗"
    text = f"[{label}] {event['code']} {event['name']}({event['market']} / {event.get('margin', '取得失敗')})\n発行額: {amount}"
    if detail.get("parse_warnings"):
        text += "\n注意: " + " / ".join(detail["parse_warnings"])
    return text


def format_split_review(event: dict[str, Any], label: str = "株式分割・判定待ち") -> str:
    detail = event.get("detail", {})
    ratio = detail.get("ratio") or "要確認"
    warnings = list(
        dict.fromkeys(
            [
                *(detail.get("parse_warnings") or []),
                *(event.get("eligibility", {}).get("reasons") or []),
            ]
        )
    )
    text = (
        f"[{label}] {event.get('code')} {event.get('name')}({event.get('market')} / {event.get('margin')})\n"
        f"分割比率: 1:{ratio}\n権利付最終日: {detail.get('rights_final_date') or '要確認'}"
    )
    return text + ("\n注意: " + " / ".join(warnings) if warnings else "")


def notify_system_safely(notifier: SlackNotifier, text: str) -> None:
    try:
        notifier.system(text)
    except Exception as exc:  # Avoid masking source failures or printing secret webhook URLs.
        print(f"System alert failed: {type(exc).__name__}")


def record_cached_source_success(state: dict[str, Any], source: str, cache_name: str) -> bool:
    fetched_at = cache_fetched_at(cache_name)
    return record_source_success(state, source, fetched_at) if fetched_at is not None else False


if __name__ == "__main__":
    raise SystemExit(main())
