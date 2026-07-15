from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
import os
from typing import Any

from src.collectors.jpx_margin import fetch_margin, lookup_margin
from src.collectors.jpx_master import fetch_master, lookup_master
from src.collectors.tdnet import Disclosure, classify_title, contains_buyback, fetch_disclosures, fetch_pdf_text
from src.core.bizday import JST, is_business_day, today_jst
from src.core.scheduler import build_bunbai_schedule, build_po_schedule, build_split_schedule
from src.core.store import (
    add_notified_id,
    find_events,
    has_notified,
    load_state,
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
    changed = False

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

    try:
        disclosures = sorted(fetch_disclosures(target_date), key=lambda item: (item.announced_at, item.id))
    except Exception as exc:
        notify_system_safely(notifier, f"TDnet取得失敗: {exc}")
        raise

    health_changed, should_alert = record_source_result(state, "tdnet", target_date, len(disclosures))
    changed |= health_changed
    if should_alert:
        notify_system_safely(notifier, "TDnet取得件数が3営業日以上連続で0件です。取得元の仕様変更を確認してください")
    if health_changed and not args.dry_run:
        save_state(state)

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
            item_changed = False
            if "buyback" in classes:
                item_changed |= handle_buyback(disclosure, state, notifier)
                known_buybacks.add((disclosure.code, disclosure.announced_at.date().isoformat()))
            elif "po_pricing" in classes:
                item_changed |= handle_po_pricing(disclosure, state, notifier, master, margin)
            elif "po" in classes:
                item_changed |= handle_po(disclosure, state, notifier, master, margin)
            elif "bunbai" in classes:
                item_changed |= handle_bunbai(disclosure, state, notifier, master, margin)
            elif "cb" in classes:
                if not margin:
                    notify_system_safely(notifier, f"CB判定保留: {disclosure.code} 信用区分を取得できません")
                    continue
                item_changed |= handle_cb(disclosure, state, notifier, master, margin, known_buybacks)
            elif "split" in classes:
                item_changed |= handle_split(disclosure, state, notifier, master, margin)

            item_changed |= add_notified_id(state, disclosure.id)
            changed |= item_changed
            if item_changed and not args.dry_run:
                save_state(state)
        except Exception as exc:
            if changed and not args.dry_run:
                save_state(state)
            message = f"TDnet処理失敗: {disclosure.code} {disclosure.title}: {exc}"
            notify_system_safely(notifier, message)
            raise RuntimeError(message) from exc

    changed |= trim_notified_ids(state)
    if changed and not args.dry_run:
        save_state(state)
    return 0


def base_event(disclosure: Disclosure, event_type: str, master: dict[str, Any], margin: dict[str, str]) -> dict[str, Any]:
    master_item = lookup_master(master, disclosure.code, fallback_name=disclosure.name)
    return {
        "id": f"{event_type}-{disclosure.code}-{disclosure.announced_at.date().isoformat()}",
        "type": event_type,
        "code": disclosure.code,
        "name": master_item["name"] or disclosure.name,
        "market": master_item["market"],
        "margin": lookup_margin(margin, disclosure.code),
        "announced_at": disclosure.announced_at.astimezone(JST).isoformat(),
        "detail": {},
        "schedule": [],
        "pdf_url": disclosure.pdf_url,
        "source_title": disclosure.title,
    }


def handle_po(disclosure: Disclosure, state: dict[str, Any], notifier: SlackNotifier, master: dict[str, Any], margin: dict[str, str]) -> bool:
    text = fetch_pdf_text(disclosure.pdf_url)
    event = base_event(disclosure, "po", master, margin)
    event["detail"] = parse_po_details(disclosure.title, text, disclosure.announced_at.date())
    if event["detail"].get("pricing_date"):
        event["schedule"] = build_po_schedule(event["detail"]["pricing_date"], event["detail"].get("settlement_date"))
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
    candidates = find_events(
        state,
        event_type="po",
        code=disclosure.code,
        predicate=lambda event: not event.get("detail", {}).get("pricing_date_confirmed"),
    )
    if not candidates:
        text = fetch_pdf_text(disclosure.pdf_url)
        event = base_event(disclosure, "po", master, margin)
        detail = parse_po_details(disclosure.title, text, disclosure.announced_at.date())
        pricing_date = disclosure.announced_at.date().isoformat()
        detail["pricing_date"] = pricing_date
        detail["pricing_date_confirmed"] = True
        event["detail"] = detail
        event["schedule"] = build_po_schedule(pricing_date, detail.get("settlement_date"))
        notifier.send(
            "po",
            format_po_announcement(event) + "\n⚠️ 当初発表を取得できなかったため価格決定情報から復元",
            header="PO価格決定（復元）",
            pdf_url=event.get("pdf_url"),
        )
        upsert_event(state, event)
        return True
    event = sorted(candidates, key=lambda item: item.get("announced_at", ""), reverse=True)[0]
    old_schedule = event.get("schedule", [])
    detail = event.setdefault("detail", {})
    pricing_date = disclosure.announced_at.date().isoformat()
    detail["pricing_date"] = pricing_date
    detail["pricing_date_confirmed"] = True
    event["schedule"] = build_po_schedule(pricing_date, detail.get("settlement_date"), old_schedule=old_schedule)
    return True


def handle_bunbai(disclosure: Disclosure, state: dict[str, Any], notifier: SlackNotifier, master: dict[str, Any], margin: dict[str, str]) -> bool:
    text = fetch_pdf_text(disclosure.pdf_url)
    event = base_event(disclosure, "bunbai", master, margin)
    event["detail"] = parse_bunbai_details(text, disclosure.announced_at.date())
    if event["detail"].get("execution_date"):
        event["schedule"] = build_bunbai_schedule(event["detail"]["execution_date"])
    else:
        notify_system_safely(notifier, f"立会外分売実施日の抽出失敗: {disclosure.code} {disclosure.title}")
    notifier.send("bunbai", format_bunbai_announcement(event), header="立会外分売発表", pdf_url=event.get("pdf_url"))
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
    text = fetch_pdf_text(disclosure.pdf_url)
    same_day_key = (disclosure.code, disclosure.announced_at.date().isoformat())
    if same_day_key in same_day_buybacks or contains_buyback(disclosure.title) or contains_buyback(text):
        return True
    event = base_event(disclosure, "cb", master, margin)
    event["detail"] = {"amount": extract_cb_amount(text), "canceled": False}
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
    text = fetch_pdf_text(disclosure.pdf_url)
    event = base_event(disclosure, "split", master, margin)
    event["detail"] = parse_split_details(text, disclosure.announced_at.date())
    if event["detail"].get("effective_date"):
        event["schedule"] = build_split_schedule(event["detail"]["effective_date"])
    else:
        notify_system_safely(notifier, f"株式分割効力発生日の抽出失敗: {disclosure.code} {disclosure.title}")
    _, changed = upsert_event(state, event)
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
    detail = event.get("detail", {})
    kind = {"offering": "公募増資", "secondary": "売出し", "both": "公募増資+売出し"}.get(detail.get("po_kind"), "要確認")
    size = "取得失敗" if detail.get("size_oku") is None else f"約{detail['size_oku']:,}億円"
    dilution = "" if detail.get("dilution_pct") is None else f"(発行済比 {detail['dilution_pct']}%)"
    pricing = detail.get("pricing_date_raw") or detail.get("pricing_date") or "取得失敗"
    settlement = detail.get("settlement_date") or "取得失敗"
    if detail.get("settlement_estimated"):
        settlement += "(暫定)"
    return (
        f"[PO発表] {event['code']} {event['name']}({event['market']} / {event['margin']})\n"
        f"種別: {kind}\n"
        f"吸収規模: {size}{dilution}\n"
        f"価格決定日: {pricing}\n"
        f"想定受渡日: {settlement}"
    )


def format_bunbai_announcement(event: dict[str, Any]) -> str:
    execution = event.get("detail", {}).get("execution_date") or "要確認"
    return f"[立会外分売発表] {event['code']} {event['name']}({event['market']} / {event['margin']})\n分売実施日: {execution}"


def format_cb_announcement(event: dict[str, Any]) -> str:
    amount = event.get("detail", {}).get("amount") or "取得失敗"
    return f"[CB発表] {event['code']} {event['name']}({event['market']} / 貸借)\n発行額: {amount}"


def notify_system_safely(notifier: SlackNotifier, text: str) -> None:
    try:
        notifier.system(text)
    except Exception as exc:  # Avoid masking source failures or printing secret webhook URLs.
        print(f"System alert failed: {type(exc).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
