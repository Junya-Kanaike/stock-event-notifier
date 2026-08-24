from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
import os
from typing import Any

from src.collectors.jpx_margin import fetch_margin, lookup_margin
from src.collectors.jpx_master import fetch_master, lookup_master
from src.collectors.tdnet import Disclosure, classify_title, contains_buyback, fetch_disclosures, fetch_pdf_text
from src.core.bizday import JST, is_business_day, prev_business_day, today_jst
from src.core.dateparse import clean_text, find_dates
from src.core.po import format_po_message, merge_po_details, refresh_po_missing_fields
from src.core.reconcile import reconcile_event_state
from src.core.scheduler import build_bunbai_schedule, build_po_schedule, build_split_schedule
from src.core.store import (
    add_notified_id,
    clear_disclosure_failure,
    find_events,
    has_notified,
    load_state,
    record_disclosure_failure,
    record_source_result,
    save_state,
    trim_notified_ids,
    upsert_event,
)
from src.notifiers.slack import SlackNotifier
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
    state = load_state()
    if args.dry_run:
        state = deepcopy(state)
    changed = reconcile_event_state(state)
    changed |= recover_split_events(state, notifier)
    changed |= notify_unresolved_split_events(state, notifier)
    if changed and not args.dry_run:
        save_state(state)

    try:
        master = fetch_master()
    except Exception as exc:
        master = {}
        notify_system_safely(notifier, f"JPX銘柄マスター取得失敗: {exc}")

    try:
        margin = fetch_margin()
    except Exception as exc:
        margin = {}
        notify_system_safely(notifier, f"JPX信用区分取得失敗: {exc}")

    target_dates = poll_target_dates(target_date, explicit_date=bool(args.date))
    disclosures, source_failures = fetch_poll_disclosures(target_dates, notifier)
    changed |= recover_missing_event_markets(state, disclosures)

    if len(source_failures) < len(target_dates):
        health_changed, should_alert = record_source_result(state, "tdnet", target_date, len(disclosures))
        changed |= health_changed
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

    changed |= trim_notified_ids(state)
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
    known_buybacks: set[tuple[str, str]] = set()
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
        if margin:
            changed |= handle_cb(disclosure, state, notifier, master, margin, known_buybacks)
        else:
            notify_system_safely(notifier, f"CB判定保留: {disclosure.code} 信用区分を取得できません")
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
    text, pdf_warning = fetch_disclosure_text_safely(disclosure, notifier)
    event = base_event(disclosure, "po", master, margin)
    event["detail"] = parse_po_details(disclosure.title, text, disclosure.announced_at.date())
    add_parse_warning(event["detail"], pdf_warning)
    if event["detail"].get("pricing_date"):
        event["schedule"] = build_po_schedule(event["detail"]["pricing_date"], event["detail"].get("settlement_date"))
    else:
        notify_system_safely(notifier, f"PO価格決定日の抽出失敗: {disclosure.code} {disclosure.title}")
    notifier.send("po", format_po_announcement(event), header="PO発表", pdf_url=event.get("pdf_url"))
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
        pricing_date = disclosure.announced_at.date().isoformat()
        detail["pricing_date"] = pricing_date
        detail["pricing_date_end"] = pricing_date
        detail["pricing_date_confirmed"] = True
        detail["pricing_date_status"] = "confirmed"
        add_parse_warning(detail, "当初発表を取得できず価格決定資料から復元")
        refresh_po_missing_fields(detail)
        event["detail"] = detail
        event["schedule"] = build_po_schedule(pricing_date, detail.get("settlement_date"))
        notifier.send(
            "po",
            format_po_message(event, "PO価格決定（復元）"),
            header="PO価格決定（復元）",
            pdf_url=event.get("latest_pdf_url"),
        )
        upsert_event(state, event)
        return True
    event = sorted(candidates, key=lambda item: item.get("announced_at", ""), reverse=True)[0]
    updated = deepcopy(event)
    old_schedule = updated.get("schedule", [])
    detail = merge_po_details(updated.get("detail", {}), parsed_detail)
    pricing_date = disclosure.announced_at.date().isoformat()
    detail["pricing_date"] = pricing_date
    detail["pricing_date_end"] = pricing_date
    detail["pricing_date_confirmed"] = True
    detail["pricing_date_status"] = "confirmed"
    refresh_po_missing_fields(detail)
    updated["detail"] = detail
    updated["latest_pdf_url"] = disclosure.pdf_url
    append_related_disclosure(updated, disclosure, "pricing")
    updated["schedule"] = build_po_schedule(pricing_date, detail.get("settlement_date"), old_schedule=old_schedule)
    notifier.send(
        "po",
        format_po_message(updated, "PO価格決定"),
        header="PO価格決定",
        pdf_url=disclosure.pdf_url,
    )
    upsert_event(state, updated)
    return True


def handle_po_correction(
    disclosure: Disclosure,
    state: dict[str, Any],
    notifier: SlackNotifier,
    master: dict[str, Any],
    margin: dict[str, str],
) -> bool:
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
            refresh_po_missing_fields(parsed_detail)
            updated["detail"] = parsed_detail
        if updated["detail"].get("pricing_date"):
            updated["schedule"] = build_po_schedule(
                updated["detail"]["pricing_date"], updated["detail"].get("settlement_date")
            )

    notifier.send(
        "po",
        format_po_message(updated, "PO訂正"),
        header="PO訂正",
        pdf_url=disclosure.pdf_url,
    )
    upsert_event(state, updated)
    return True


def handle_bunbai(disclosure: Disclosure, state: dict[str, Any], notifier: SlackNotifier, master: dict[str, Any], margin: dict[str, str]) -> bool:
    text, pdf_warning = fetch_disclosure_text_safely(disclosure, notifier)
    parsed_detail = parse_bunbai_details(text, disclosure.announced_at.date())
    add_parse_warning(parsed_detail, pdf_warning)
    followup = is_bunbai_followup_title(disclosure.title)
    existing = find_bunbai_event_for_update(state, disclosure.code, parsed_detail.get("execution_date"), followup)
    if existing:
        event = deepcopy(existing)
        detail = event.setdefault("detail", {})
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

    if event["detail"].get("execution_date"):
        event["schedule"] = build_bunbai_schedule(
            event["detail"]["execution_date"], old_schedule=event.get("schedule", [])
        )
    else:
        notify_system_safely(notifier, f"立会外分売実施日の抽出失敗: {disclosure.code} {disclosure.title}")
    label = "立会外分売更新" if existing else "立会外分売発表"
    notifier.send("bunbai", format_bunbai_announcement(event, label), header=label, pdf_url=disclosure.pdf_url)
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
    if lookup_margin(margin, disclosure.code) != "貸借":
        return True
    text, pdf_warning = fetch_disclosure_text_safely(disclosure, notifier)
    same_day_key = (disclosure.code, disclosure.announced_at.date().isoformat())
    if same_day_key in same_day_buybacks or contains_buyback(disclosure.title) or contains_buyback(text):
        return True
    event = base_event(disclosure, "cb", master, margin)
    event["detail"] = {"amount": extract_cb_amount(text), "canceled": False}
    add_parse_warning(event["detail"], pdf_warning)
    notifier.send("cb", format_cb_announcement(event), header="CB発表", pdf_url=event.get("pdf_url"))
    upsert_event(state, event)
    return True


def handle_split(
    disclosure: Disclosure,
    state: dict[str, Any],
    notifier: SlackNotifier,
    master: dict[str, Any],
    margin: dict[str, str],
) -> bool:
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
    if event["detail"].get("effective_date"):
        event["schedule"] = build_split_schedule(
            event["detail"]["effective_date"], old_schedule=event.get("schedule", [])
        )
    else:
        notify_system_safely(notifier, f"株式分割効力発生日の抽出失敗: {disclosure.code} {disclosure.title}")
        notifier.send(
            "split",
            format_split_review(event),
            header="株式分割 要確認",
            pdf_url=disclosure.pdf_url,
        )
        event["detail"]["review_notified"] = True
    _, changed = upsert_event(state, event)
    return changed


def recover_split_events(state: dict[str, Any], notifier: SlackNotifier) -> bool:
    """Reparse split events damaged by old classification or ratio parsing."""
    changed = False
    for event in find_events(
        state,
        event_type="split",
        predicate=lambda item: bool(item.get("detail", {}).get("recovery_needed")),
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
            if detail.get("effective_date"):
                event["schedule"] = build_split_schedule(
                    detail["effective_date"], old_schedule=event.get("schedule", [])
                )
            if not detail.get("ratio") or not detail.get("effective_date"):
                raise ValueError("分割比率または効力発生日を抽出できません")
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
    """Send the review notice that old parser failures previously omitted."""
    changed = False
    for event in find_events(
        state,
        event_type="split",
        predicate=lambda item: not item.get("detail", {}).get("effective_date")
        and not item.get("detail", {}).get("review_notified"),
    ):
        try:
            notifier.send(
                "split",
                format_split_review(event),
                header="株式分割 要確認",
                pdf_url=event.get("latest_pdf_url") or event.get("pdf_url"),
            )
        except Exception as exc:
            notify_system_safely(
                notifier,
                f"株式分割の要確認通知失敗: {event.get('code')} {type(exc).__name__}",
            )
            continue
        event.setdefault("detail", {})["review_notified"] = True
        changed = True
    return changed


def handle_buyback(disclosure: Disclosure, state: dict[str, Any], notifier: SlackNotifier) -> bool:
    changed = False
    disclosure_day = disclosure.announced_at.date().isoformat()
    for event in find_events(state, event_type="cb", code=disclosure.code):
        if event.get("announced_at", "")[:10] != disclosure_day:
            continue
        detail = event.setdefault("detail", {})
        if detail.get("canceled"):
            continue
        notifier.send("cb", f"[CB取消] {event.get('code')} {event.get('name')}: 自社株買い同時発表を確認", header="CB取消")
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
    return format_po_message(event, "PO発表")


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
    return any(marker in normalized for marker in ["分売実施", "分売終了", "分売条件", "訂正", "変更"])


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
    return max(pending, key=lambda event: event.get("announced_at", ""), default=None)


def find_split_event_for_update(state: dict[str, Any], disclosure: Disclosure) -> dict[str, Any] | None:
    normalized = clean_text(disclosure.title).replace(" ", "")
    if not any(marker in normalized for marker in ["訂正", "変更"]):
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


def format_cb_announcement(event: dict[str, Any]) -> str:
    detail = event.get("detail", {})
    amount = detail.get("amount") or "取得失敗"
    text = f"[CB発表] {event['code']} {event['name']}({event['market']} / 貸借)\n発行額: {amount}"
    if detail.get("parse_warnings"):
        text += "\n注意: " + " / ".join(detail["parse_warnings"])
    return text


def format_split_review(event: dict[str, Any]) -> str:
    detail = event.get("detail", {})
    ratio = detail.get("ratio") or "要確認"
    warnings = " / ".join(detail.get("parse_warnings") or ["効力発生日を抽出できません"])
    return (
        f"[株式分割 要確認] {event.get('code')} {event.get('name')}({event.get('market')})\n"
        f"分割比率: 1:{ratio}\n注意: {warnings}"
    )


def notify_system_safely(notifier: SlackNotifier, text: str) -> None:
    try:
        notifier.system(text)
    except Exception as exc:  # Avoid masking source failures or printing secret webhook URLs.
        print(f"System alert failed: {type(exc).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
