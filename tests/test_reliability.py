from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from src.collectors import tdnet
from src.core.bizday import JST
from src.core.operations import record_tdnet_date, record_run, resolve_expired_notification
from src.core.po import refresh_calculated_po_size
from src.core.reconcile import reconcile_event_state
from src.core.scheduler import build_po_schedule, build_split_schedule, due_notifications
from src.notifiers.slack import SlackNotifier
from src.parsers.po_pdf import parse_po_details, extract_confirmed_prices
from src.parsers.split_pdf import parse_split_details
from src.run_daily import send_pending_system_summaries, send_due_notifications
from src.run_poll import fetch_poll_disclosures, recover_po_calculations


NOW = datetime(2026, 9, 16, 8, tzinfo=JST)
FIXTURE = Path(__file__).parent / "fixtures/tdnet/3455_pricing_20260826.txt"


class ReliabilityTest(unittest.TestCase):
    def test_slack_retry_crossing_open_refreshes_text_and_delivery_evidence(self):
        event = {"id": "po-retry", "type": "po", "detail": {},
                 "schedule": [{"date": "2026-09-16", "label": "pricing_day", "sent": False,
                               "action_cutoff_jst": "09:00"}]}
        before = NOW.replace(hour=8, minute=59, second=59)
        after = NOW.replace(hour=9, minute=0, second=2)
        payloads = []
        def post(url, *, json, timeout):
            payloads.append(json["text"])
            if len(payloads) == 1:
                raise RuntimeError("temporary")
            return Mock()
        state = {"events": [event]}
        with patch.dict("os.environ", {"SLACK_WEBHOOK_PO": "https://example.test/mock"}), patch(
            "src.run_daily.now_jst", side_effect=[before, before, before, after]
        ), patch("src.run_daily.save_state"), patch("requests.post", side_effect=post), patch("src.notifiers.slack.time.sleep"):
            self.assertEqual(send_due_notifications(state, SlackNotifier(dry_run=False), before, dry_run=False), 1)
        self.assertIn("寄り付きで買う", payloads[0])
        self.assertNotIn("寄り付きで買う", payloads[1])
        self.assertTrue(state["notification_log"][-1]["reference_only"])
        self.assertEqual(event["schedule"][0]["sent_at"], after.isoformat())

    def test_slow_job_crossing_open_uses_send_time_cutoff(self):
        event = {"id": "po-clock", "type": "po", "detail": {},
                 "schedule": [{"date": "2026-09-16", "label": "pricing_day",
                               "sent": False, "action_cutoff_jst": "09:00"}]}
        before = NOW.replace(hour=8, minute=59)
        after = NOW.replace(hour=9, minute=0)
        notifier = Mock()
        with patch("src.run_daily.now_jst", side_effect=[before, after]), patch("src.run_daily.save_state"):
            send_due_notifications({"events": [event]}, notifier, before, dry_run=False)
        message = notifier.send.call_args.args[1]
        self.assertIn("参考通知", message)
        self.assertNotIn("寄り付きで買う", message)
        self.assertEqual(event["schedule"][0]["sent_at"], after.isoformat())

    def test_verified_hcm_state_repair_is_idempotent(self):
        event = {"id": "po-3455-2026-08-19", "type": "po", "code": "3455",
                 "announced_at": "2026-08-19", "market": "東証REIT", "margin": "貸借",
                 "detail": {"parser_version": 2, "pricing_date": "2026-08-26",
                            "offer_price_yen": 8836367280}, "schedule": []}
        state = {"events": [event]}
        self.assertTrue(reconcile_event_state(state, as_of=NOW.date()))
        updated = state["events"][0]
        self.assertEqual(updated["detail"]["confirmed_size_yen"], 9278100000)
        self.assertEqual(len(updated["repair_history"]), 1)
        self.assertEqual(updated["schedule"][-1]["date"], "2026-10-06")
        self.assertFalse(reconcile_event_state(state, as_of=NOW.date()))

    def test_distinct_dated_bunbai_cycles_are_never_merged(self):
        from src.core.reconcile import _same_bunbai_cycle
        first = {"detail": {"execution_date": "2026-09-01"}, "announced_at": "2026-08-25"}
        second = {"detail": {"execution_date": "2026-09-09"}, "announced_at": "2026-09-01"}
        self.assertFalse(_same_bunbai_cycle(first, second))

    def test_bunbai_confirmed_without_date_can_merge_into_dated_record(self):
        from src.core.reconcile import _same_bunbai_cycle
        first = {"detail": {"execution_date_confirmed": True}, "announced_at": "2026-08-25"}
        second = {"detail": {"execution_date": "2026-09-01", "execution_date_confirmed": True}, "announced_at": "2026-09-01"}
        self.assertTrue(_same_bunbai_cycle(first, second))

    def test_record_date_ignores_record_announcement_date(self):
        parsed = parse_split_details("基準日公告日（予定）2026年9月10日 基準日2026年9月30日 効力発生日2026年10月1日", NOW.date())
        self.assertEqual(parsed["record_date"], "2026-09-30")
        self.assertEqual(parsed["rights_final_date"], "2026-09-28")
        self.assertEqual(parsed["ex_right_date"], "2026-09-29")

    def test_missing_system_webhook_cannot_mark_summary_sent(self):
        state = {"events": []}
        with patch.dict("os.environ", {}, clear=True):
            send_pending_system_summaries(state, SlackNotifier(dry_run=False), NOW.replace(hour=21), failures=[])
        self.assertNotIn("last_sent_date", state["daily_system_summary"])

    def test_exact_schedule_preserves_all_delivery_metadata(self):
        old = build_po_schedule("2026-09-16", "2026-09-24")
        old[0].update(sent=True, sent_at=NOW.isoformat(), sent_by="123",
                      reference_only=False, suppressed_reason="reviewed")
        rebuilt = build_po_schedule("2026-09-16", "2026-09-24", old_schedule=old)
        self.assertEqual(rebuilt[0], old[0])

    def test_changed_date_does_not_inherit_resolution(self):
        old = build_po_schedule("2026-09-01", None)
        old[0]["resolution"] = {"status": "missed_reviewed"}
        rebuilt = build_po_schedule("2026-09-02", None, old_schedule=old)
        self.assertNotIn("resolution", rebuilt[0])

    def split(self):
        return {"id": "split-4062", "type": "split", "code": "4062",
                "market": "プライム", "margin": "貸借", "announced_at": "2026-08-04",
                "detail": {"ratio": "4", "rights_final_date": "2026-09-28",
                           "rights_final_confirmed": True},
                "schedule": build_split_schedule("2026-09-28")}

    def test_future_unattributed_sent_flag_is_repaired_once(self):
        event = self.split()
        event["schedule"][0]["sent"] = True
        state = {"events": [event]}
        self.assertTrue(reconcile_event_state(state, as_of=NOW.date()))
        repaired = state["events"][0]
        self.assertFalse(repaired["schedule"][0]["sent"])
        self.assertEqual(len(repaired["schedule_history"]), 1)
        self.assertFalse(reconcile_event_state(state, as_of=NOW.date()))

    def test_future_timestamped_or_suppressed_record_is_not_reset(self):
        event = self.split()
        event["schedule"][0].update(sent=True, sent_at=NOW.isoformat())
        event["schedule"][1].update(sent=True, suppressed_reason="manual")
        state = {"events": [event]}
        reconcile_event_state(state, as_of=NOW.date())
        self.assertTrue(all(x["sent"] for x in state["events"][0]["schedule"][:2]))

    def test_reit_real_pdf_counts_price_and_size(self):
        detail = parse_po_details("新投資口発行及び投資口売出しに係る価格等の決定",
                                  FIXTURE.read_text(), date(2026, 8, 26))
        self.assertEqual(detail["public_offering_shares"], 92858)
        self.assertEqual(detail["secondary_sale_shares"], 0)
        self.assertEqual(detail["oa_shares"], 4642)
        self.assertEqual(detail["offer_price_yen"], 95160)
        self.assertEqual(detail["total_offered_shares"], 97500)
        self.assertEqual(detail["settlement_date"], "2026-09-02")
        self.assertAlmostEqual(detail["size_oku"], 92.781)
        refresh_calculated_po_size(detail)
        self.assertEqual(detail["confirmed_size_yen"], 9278100000)

    def test_interleaved_aggregate_is_never_unit_price(self):
        result = extract_confirmed_prices("発行価格 8,836,367,280円（募集価格）の総額")
        self.assertIsNone(result["offer_price_yen"])

    def test_recovery_continues_after_one_pdf_fails_and_throttles_retry(self):
        event = {"id": "po-3455", "type": "po", "code": "3455", "market": "東証REIT",
                 "margin": "貸借", "announced_at": "2026-08-19T15:45:00+09:00",
                 "pdf_url": "https://example.test/missing.pdf", "detail": {},
                 "related_disclosures": [{"pdf_url": "https://example.test/pricing.pdf",
                     "title": "新投資口発行及び投資口売出しに係る価格等の決定",
                     "announced_at": "2026-08-26T17:00:00+09:00"}]}
        state = {"events": [event]}
        with patch("src.run_poll.fetch_pdf_text", side_effect=[RuntimeError("expired"), FIXTURE.read_text()]) as get, patch(
            "src.run_poll.apply_po_reference_price", return_value=False
        ):
            recover_po_calculations(state, SlackNotifier(dry_run=True), now=NOW)
            self.assertEqual(event["detail"]["confirmed_size_yen"], 9278100000)
            self.assertEqual(event["eligibility"]["status"], "eligible")
            self.assertEqual(event["schedule"][-2]["date"], "2026-10-05")
            recover_po_calculations(state, SlackNotifier(dry_run=True), now=NOW)
            self.assertEqual(get.call_count, 2)

    def test_source_failure_does_not_refresh_success_timestamp(self):
        state = {}
        record_tdnet_date(state, NOW.date(), NOW, 30)
        record_tdnet_date(state, NOW.date(), NOW + timedelta(minutes=10), 5, "page 2 failed")
        result = state["source_health"]["tdnet"]["by_date"][NOW.date().isoformat()]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["last_success_at"], NOW.isoformat())
        record_tdnet_date(state, NOW.date(), NOW + timedelta(minutes=20), 40)
        self.assertEqual(state["source_health"]["tdnet"]["by_date"][NOW.date().isoformat()]["status"], "success")

    def test_failed_workflow_does_not_erase_last_success(self):
        state = {}
        record_run(state, "poll_tdnet", NOW, NOW)
        record_run(state, "poll_tdnet", NOW, NOW + timedelta(minutes=10), failed=True)
        self.assertEqual(state["workflow_health"]["poll_tdnet"]["last_success_at"], NOW.isoformat())

    def test_midnight_summary_recovers_previous_day_without_current_counts(self):
        state = {"events": [], "daily_system_summary": {"last_sent_date": "2026-09-14"},
                 "notification_stats": {"2026-09-15": {"success": 7, "failure": 1}}}
        notifier = SlackNotifier(dry_run=True)
        self.assertTrue(send_pending_system_summaries(state, notifier, NOW, failures=[]))
        self.assertEqual(state["daily_system_summary"]["last_sent_date"], "2026-09-15")
        text = notifier.sent_messages[0]["payload"]["text"]
        self.assertIn("当日の通知成功: 7件", text)
        self.assertIn("遅延集約", text)
        self.assertFalse(send_pending_system_summaries(state, notifier, NOW, failures=[]))

    def test_summary_failure_retries_same_day(self):
        state = {"events": [], "daily_system_summary": {"last_sent_date": "2026-09-14"}}
        notifier = SlackNotifier(dry_run=True)
        with patch.object(notifier, "system", side_effect=RuntimeError("down")):
            send_pending_system_summaries(state, notifier, NOW, failures=[])
        self.assertEqual(state["daily_system_summary"]["last_sent_date"], "2026-09-14")
        send_pending_system_summaries(state, notifier, NOW, failures=[])
        self.assertEqual(state["daily_system_summary"]["last_sent_date"], "2026-09-15")

    def test_delivery_failure_stays_unsent_and_next_run_succeeds(self):
        event = {"id": "ipo-1", "type": "ipo", "code": "1234", "detail": {},
                 "schedule": [{"date": "2026-09-16", "label": "listing_day", "sent": False}]}
        state = {"events": [event]}
        notifier = SlackNotifier(dry_run=True)
        with patch.object(notifier, "send", side_effect=RuntimeError("down")):
            self.assertEqual(send_due_notifications(state, notifier, NOW, dry_run=True), 0)
        self.assertFalse(event["schedule"][0]["sent"])
        self.assertEqual(state["notification_log"][-1]["outcome"], "failed")
        self.assertEqual(send_due_notifications(state, notifier, NOW, dry_run=True), 1)
        self.assertEqual(state["notification_log"][-1]["outcome"], "accepted")
        self.assertIn("sent_by", event["schedule"][0])

    def test_expired_resolution_does_not_claim_sent_or_leak_into_changed_date(self):
        event = {"type": "po", "detail": {}, "schedule": build_po_schedule("2026-09-01", None)}
        self.assertTrue(resolve_expired_notification(event, "2026-09-01", "pricing_day", NOW, "audit reviewed"))
        self.assertFalse(event["schedule"][0]["sent"])
        self.assertFalse(resolve_expired_notification(event, "2026-09-01", "pricing_day", NOW, "audit reviewed"))
        self.assertNotIn(event["schedule"][0], [x.schedule_item for x in due_notifications({"events": [event]}, NOW)])

    def test_partial_disclosures_survive_poll_error_and_date_remains_failed(self):
        item = tdnet.Disclosure("x", "7203", "A", "株式分割", NOW)
        state = {}
        with patch("src.run_poll.fetch_disclosures", side_effect=tdnet.PartialDisclosureError("page 2", [item])):
            rows, failures = fetch_poll_disclosures([NOW.date()], SlackNotifier(dry_run=True), state=state)
        self.assertEqual(rows, [item])
        self.assertEqual(len(failures), 1)
        self.assertEqual(state["source_health"]["tdnet"]["by_date"]["2026-09-16"]["status"], "partial")

    def test_capped_api_rows_survive_broken_html(self):
        item = tdnet.Disclosure("x", "7203", "A", "株式分割", NOW)
        with patch("src.collectors.tdnet.fetch_yanoshin_disclosures", return_value=tdnet.DisclosureBatch([item], 300)), patch(
            "src.collectors.tdnet.fetch_tdnet_html_disclosures", side_effect=RuntimeError("bad schema")
        ), self.assertRaises(tdnet.PartialDisclosureError) as caught:
            tdnet.fetch_disclosures(NOW.date())
        self.assertEqual(caught.exception.disclosures, [item])

    def test_html_valid_rows_without_heading_are_supported(self):
        html = '<table><tr><td>15:30</td><td>72030</td><td>A</td><td><a href="a.pdf">株式分割</a></td></tr></table>'
        with patch("src.collectors.tdnet.request_get", return_value=html.encode()):
            self.assertEqual(len(tdnet.fetch_tdnet_html_disclosures("20260916")), 1)

    def test_bad_page_does_not_lose_other_pages(self):
        first = '<a href="I_list_002_20260916.html">2</a><a href="I_list_003_20260916.html">3</a><table><tr><td>15:30</td><td>72030</td><td>A</td><td><a href="a.pdf">株式分割</a></td></tr></table>'
        third = '<table><tr><td>16:00</td><td>67580</td><td>B</td><td><a href="b.pdf">株式分割</a></td></tr></table>'
        with patch("src.collectors.tdnet.request_get", side_effect=[first.encode(), b"bad", third.encode()]), self.assertRaises(
            tdnet.PartialDisclosureError
        ) as caught:
            tdnet.fetch_tdnet_html_disclosures("20260916")
        self.assertEqual([x.code for x in caught.exception.disclosures], ["7203", "6758"])


if __name__ == "__main__":
    unittest.main()
