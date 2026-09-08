from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from unittest.mock import patch

from src.collectors.tdnet import Disclosure
from src.core.bizday import JST
from src.core.po import format_po_message
from src.core.scheduler import (
    build_bunbai_schedule,
    build_ipo_schedule,
    build_po_schedule,
    build_split_schedule,
    due_notifications,
)
from src.notifiers.slack import SlackNotifier
from src.core.transitions import eligibility_transition
from src.run_daily import send_daily_system_summary
from src.run_poll import format_bunbai_announcement, format_cb_announcement, handle_cb


def event(event_type: str, *, code: str = "7203", name: str = "検証銘柄") -> dict:
    return {
        "id": f"{event_type}-{code}-matrix",
        "type": event_type,
        "code": code,
        "name": name,
        "market": "プライム",
        "margin": "貸借",
        "eligibility": {"status": "eligible", "reasons": []},
        "detail": {
            "po_kind": "both",
            "public_offering_shares": 1_000_000,
            "secondary_sale_shares": 2_000_000,
            "oa_shares": 300_000,
            "total_offered_shares": 3_300_000,
            "reference_close_yen": 2_500,
            "reference_close_date": "2026-09-08",
            "effective_size_yen": 8_250_000_000,
            "size_oku": 82.5,
            "size_status": "estimated",
            "po_threshold_ever_met": True,
            "pricing_date": "2026-09-09",
            "settlement_date": "2026-09-17",
            "ratio": "2",
            "rights_final_date": "2026-09-25",
            "rights_final_confirmed": True,
        },
        "schedule": [],
    }


def send_due(
    notifier: SlackNotifier,
    base: dict,
    item: dict,
    at: datetime,
    *,
    contains: str,
    excludes: tuple[str, ...] = (),
) -> None:
    current = deepcopy(base)
    current["schedule"] = [deepcopy(item)]
    due = due_notifications({"events": [current]}, at)
    assert len(due) == 1, (current["type"], item, at, due)
    assert contains in due[0].text, due[0].text
    for value in excludes:
        assert value not in due[0].text, due[0].text
    notifier.send(current["type"], due[0].text)


def main() -> int:
    notifier = SlackNotifier(dry_run=True)

    po = event("po")
    notifier.send("po", format_po_message(po, "PO予定通知"))
    assert "寄り付き" not in notifier.sent_messages[-1]["payload"]["text"]
    po_schedule = build_po_schedule("2026-09-09", "2026-09-17")
    for item in po_schedule:
        send_due(
            notifier,
            po,
            item,
            datetime.fromisoformat(f"{item['date']}T08:00:00+09:00"),
            contains="寄り付きで半分売る" if item["label"] in {"pricing_day+25bd", "pricing_day+26bd"} else "寄り付きで買う",
        )
    send_due(
        notifier,
        po,
        po_schedule[0],
        datetime(2026, 9, 9, 9, 0, tzinfo=JST),
        contains="参考通知",
        excludes=("寄り付きで買う",),
    )

    ipo = event("ipo", code="603A")
    notifier.send("ipo", "[IPO判定待ち] 603A 検証銘柄\n上場日: 取得失敗")
    notifier.send("ipo", "[対象確定] 603A 検証銘柄\n上場日: 2026-09-30")
    notifier.send("ipo", "[上場日変更] 603A 検証銘柄: 2026-09-30 → 2026-10-01")
    for item in build_ipo_schedule("2026-09-30"):
        send_due(
            notifier,
            ipo,
            item,
            datetime.fromisoformat(f"{item['date']}T07:30:00+09:00"),
            contains="新規上場します",
        )

    bunbai = event("bunbai", code="4073")
    bunbai["detail"]["execution_date"] = "2026-09-10"
    notifier.send("bunbai", format_bunbai_announcement(bunbai, "立会外分売発表"))
    notifier.send("bunbai", format_bunbai_announcement(bunbai, "立会外分売 判定待ち"))
    notifier.send("bunbai", format_bunbai_announcement(bunbai, "立会外分売 対象確定"))
    notifier.send("bunbai", format_bunbai_announcement(bunbai, "実施日変更"))
    notifier.send("bunbai", "[中止] 4073 検証銘柄 (立会外分売)")
    bunbai_schedule = build_bunbai_schedule("2026-09-10")
    expected = {
        "execution-1bd": "申し込み忘れずに！",
        "execution_day": "寄り付きで買う",
        "execution+5bd": "寄り付きで売却する",
    }
    for item in bunbai_schedule:
        send_due(
            notifier,
            bunbai,
            item,
            datetime.fromisoformat(f"{item['date']}T08:00:00+09:00"),
            contains=expected[item["label"]],
        )
    send_due(
        notifier,
        bunbai,
        bunbai_schedule[1],
        datetime(2026, 9, 10, 9, 0, tzinfo=JST),
        contains="参考通知",
        excludes=("寄り付きで買う",),
    )

    cb = event("cb", code="6758")
    cb["detail"]["amount"] = None
    notifier.send("cb", format_cb_announcement(cb, "CB発表"))
    notifier.send("cb", format_cb_announcement(cb, "CB判定待ち"))
    notifier.send("cb", format_cb_announcement(cb, "CB対象確定"))
    notifier.send("cb", "⚠️ [取消] 6758 検証銘柄: 自社株買い同時発表を確認")

    split = event("split", code="9984")
    notifier.send("split", "[中止] 9984 検証銘柄 (株式分割)")
    split_schedule = build_split_schedule("2026-09-25")
    split_times = {
        "rights_final+0_after_close": datetime(2026, 9, 25, 19, 0, tzinfo=JST),
        "rights_final+1_noon": datetime(2026, 9, 28, 12, 0, tzinfo=JST),
        "rights_final+5_preopen": datetime(2026, 10, 2, 8, 0, tzinfo=JST),
    }
    split_actions = {
        "rights_final+0_after_close": "分割の空売り検討",
        "rights_final+1_noon": "大引で空売り",
        "rights_final+5_preopen": "寄り付きで買い戻し",
    }
    for item in split_schedule:
        send_due(
            notifier,
            split,
            item,
            split_times[item["label"]],
            contains=split_actions[item["label"]],
        )
    send_due(
        notifier,
        split,
        split_schedule[0],
        datetime(2026, 9, 28, 8, 0, tzinfo=JST),
        contains="参考通知",
        excludes=("分割の空売り検討",),
    )
    send_due(
        notifier,
        split,
        split_schedule[1],
        datetime(2026, 9, 28, 15, 30, tzinfo=JST),
        contains="参考通知",
        excludes=("大引で空売り",),
    )
    send_due(
        notifier,
        split,
        split_schedule[2],
        datetime(2026, 10, 2, 9, 0, tzinfo=JST),
        contains="参考通知",
        excludes=("寄り付きで買い戻し",),
    )

    excluded = event("bunbai")
    excluded["eligibility"] = {"status": "excluded", "reasons": ["東証以外"]}
    excluded["schedule"] = build_bunbai_schedule("2026-09-10")
    assert due_notifications({"events": [excluded]}, datetime(2026, 9, 10, 8, 0, tzinfo=JST)) == []
    pending_to_excluded = event("bunbai")
    pending_to_excluded["market"] = "名証メイン"
    pending_to_excluded["detail"]["pending_notified"] = True
    assert eligibility_transition(pending_to_excluded) is None
    duplicate = event("ipo")
    duplicate["schedule"] = [{"date": "2026-09-30", "label": "listing_day", "sent": True}]
    assert due_notifications({"events": [duplicate]}, date(2026, 9, 30)) == []
    preknown = Disclosure(
        id="cb-preknown",
        code="6758",
        name="検証銘柄",
        title="転換社債型新株予約権付社債の発行に関するお知らせ",
        announced_at=datetime(2026, 9, 8, 16, 0, tzinfo=JST),
        pdf_url="https://example.test/cb.pdf",
    )
    before_cb_messages = len([item for item in notifier.sent_messages if item["type"] == "cb"])
    with patch("src.run_poll.fetch_pdf_text", return_value="発行額 100億円"):
        handle_cb(
            preknown,
            {"events": []},
            notifier,
            {"6758": {"name": "検証銘柄", "market": "プライム"}},
            {"6758": "貸借"},
            {("6758", "2026-09-08")},
        )
    assert len([item for item in notifier.sent_messages if item["type"] == "cb"]) == before_cb_messages
    assert all(not item["label"].startswith("effective") for item in build_split_schedule("2026-09-25"))

    summary_state = {
        "events": [],
        "source_health": {"tdnet": {"last_success_at": "2026-09-08T19:53:00+09:00"}},
    }
    send_daily_system_summary(
        summary_state,
        notifier,
        datetime(2026, 9, 8, 20, 10, tzinfo=JST),
        failures=[],
    )

    print(
        f"MATRIX_OK sent={len(notifier.sent_messages)} "
        "silent=initially_excluded,pending_to_excluded,duplicate,cb_buyback_preknown "
        "effective_date_notifications=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
