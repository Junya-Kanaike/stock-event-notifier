from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from io import BytesIO
import unittest
from unittest.mock import patch

from src.collectors.jpx_master import parse_master_excel
from src.core.bizday import JST
from src.core.operations import stale_sources, late_delivery_warnings
from src.core.reconcile import reconcile_event_state
from src.core.scheduler import build_po_schedule, build_split_schedule, due_notifications
from src.notifiers.slack import SlackNotifier
from src.parsers.po_pdf import parse_po_details
from src.run_daily import report_cache_refresh, send_daily_system_summary


FIXTURES = Path(__file__).parent / "fixtures/tdnet"
NOW = datetime(2026, 10, 17, 16, 0, tzinfo=JST)


class OctoberDataTest(unittest.TestCase):
    def parse(self, code):
        day = 7 if code == "6407" else 2
        title = "株式の売出し" if code == "6407" else "新株式発行及び株式の売出し"
        return parse_po_details(title, (FIXTURES / f"{code}_announcement_202610{day:02}.txt").read_text(), date(2026, 10, day))

    def test_ckd_total_is_not_a_single_sellers_holding(self):
        d = self.parse("6407")
        self.assertEqual(d["secondary_sale_shares"], 3570800)
        self.assertEqual(d["oa_shares"], 535500)
        self.assertEqual(d["total_offered_shares"], 4106300)
        self.assertIsNone(d["dilution_pct"])

    def test_ckd_settlement_is_not_the_buyback_deadline(self):
        d = self.parse("6407")
        self.assertEqual((d["pricing_date"], d["pricing_date_end"]), ("2026-10-19", "2026-10-22"))
        self.assertEqual((d["settlement_date"], d["settlement_date_end"]), ("2026-10-26", "2026-10-29"))
        self.assertEqual(d["settlement_date_status"], "provisional")

    def test_infroneer_does_not_count_the_public_tranche_as_a_secondary_sale(self):
        d = self.parse("5076")
        self.assertEqual(d["public_offering_shares"], 47826100)
        self.assertEqual(d["secondary_sale_shares"], 0)
        self.assertEqual(d["total_offered_shares"], 55000000)

    def test_infroneer_dates_precede_definition_and_settlement_follows_payment(self):
        d = self.parse("5076")
        self.assertEqual((d["pricing_date"], d["pricing_date_end"]), ("2026-10-21", "2026-10-26"))
        self.assertEqual((d["settlement_date"], d["settlement_date_end"]), ("2026-10-28", "2026-11-02"))
        self.assertEqual(d["settlement_date_status"], "provisional")
        self.assertIsNone(d["dilution_pct"])

    def test_pricing_only_unit_price_is_never_one_secondary_share(self):
        d = parse_po_details("売出価格等の決定", "当社株式の売出し（引受人の買取引受けによる売出し）\n売出価格1株につき2,062円\n売出価格の総額9,806,872,000円", date(2026, 9, 9))
        self.assertIsNone(d["secondary_sale_shares"])

    def test_payment_date_alone_is_not_confirmed_settlement(self):
        d = parse_po_details("発行価格等の決定", "払込期日2026年10月27日", date(2026, 10, 21))
        self.assertIsNone(d["settlement_date"])

    def test_reviewed_misses_do_not_claim_delivery_and_do_not_repeat(self):
        from scripts.repair_reliability import repair_report
        event = {"id": "po-7966-2026-08-20", "type": "po", "code": "7966",
                 "market": "プライム", "margin": "貸借", "detail": {"po_threshold_ever_met": True},
                 "schedule": [{"date": "2026-09-01", "label": "pricing_day", "sent": False, "overdue_unresolved": True}]}
        state = {"events": [event]}
        report = repair_report(state, NOW.date(), resolve_reviewed_expired=True)
        item = state["events"][0]["schedule"][0]
        self.assertFalse(item["sent"])
        self.assertNotIn("sent_at", item)
        self.assertEqual(item["resolution"]["status"], "missed_reviewed")
        self.assertEqual(len(report["reviewed_misses"]), 1)
        self.assertEqual(repair_report(state, NOW.date(), resolve_reviewed_expired=True)["reviewed_misses"], [])

    def audited(self):
        return {"id": "po-6407-2026-10-07", "type": "po", "code": "6407",
                "announced_at": "2026-10-07T16:00:00+09:00", "market": "プライム", "margin": "貸借",
                "latest_pdf_url": "https://www.release.tdnet.info/inbs/140120261007547282.pdf",
                "detail": {"parser_version": 3, "po_kind": "secondary", "reference_close_yen": 5930,
                           "reference_close_kind": "announcement_close", "pricing_date": "2026-10-19",
                           "settlement_date": "2026-12-30"},
                "schedule": build_po_schedule("2026-10-19", "2026-12-30")}

    def test_audited_repair_preserves_matching_delivery_evidence_and_is_idempotent(self):
        e = self.audited()
        e["schedule"][0].update(sent=True, sent_at=NOW.isoformat(), sent_by="123")
        state = {"schema_version": 2, "events": [e]}
        self.assertTrue(reconcile_event_state(state, as_of=NOW.date()))
        e = state["events"][0]
        self.assertEqual(e["schedule"][0]["sent_by"], "123")
        self.assertEqual(e["detail"]["total_offered_shares"], 4106300)
        self.assertEqual(next(x for x in e["schedule"] if x["label"] == "settlement")["date"], "2026-10-26")
        self.assertFalse(reconcile_event_state(state, as_of=NOW.date()))
        self.assertEqual(len(e["repair_history"]), 1)

    def test_audited_repair_never_overwrites_a_later_disclosure(self):
        e = self.audited()
        e["latest_pdf_url"] = "https://example.test/later-correction.pdf"
        state = {"schema_version": 2, "events": [e]}
        reconcile_event_state(state, as_of=NOW.date())
        self.assertNotIn("repair_history", state["events"][0])
        self.assertEqual(state["events"][0]["detail"]["settlement_date"], "2026-12-30")

    def test_early_morning_gate_and_post_open_reference(self):
        e = self.audited()
        state = {"events": [e]}
        for hour, minute in [(0, 5), (5, 59)]:
            self.assertEqual(due_notifications(deepcopy(state), NOW.replace(day=19, hour=hour, minute=minute)), [])
        self.assertTrue(due_notifications(state, NOW.replace(day=19, hour=6)))
        self.assertTrue(all(x.reference_only for x in due_notifications(state, NOW.replace(day=19, hour=9))))

    def test_overdue_reference_is_also_quiet_before_six(self):
        e = self.audited()
        self.assertEqual(due_notifications({"events": [e]}, NOW.replace(day=20, hour=3)), [])

    def test_completed_splits_are_information_only_and_active_po_missing_schedule_is_urgent(self):
        schedule = build_split_schedule("2026-09-28")
        for x in schedule:
            x["sent"] = True
        state = {"events": [
            {"type": "split", "code": "8377", "eligibility": {"status": "eligible"},
             "detail": {"rights_final_date": "2026-09-28", "rights_final_confirmed": True}, "schedule": schedule},
            {"type": "po", "code": "5076", "eligibility": {"status": "eligible"}, "detail": {}, "schedule": []},
        ]}
        notifier = SlackNotifier(dry_run=True)
        send_daily_system_summary(state, notifier, NOW, failures=[])
        text = notifier.sent_messages[0]["payload"]["text"]
        self.assertIn("要対応: PO 5076", text)
        self.assertIn("完了済み案件の補足情報不足: 分割 1件", text)
        self.assertIn("株式分割未確定: 0件", text)
        self.assertNotIn("split 1件", text)

    def test_cache_fallback_is_reported_as_a_processing_failure(self):
        failures = []
        previous = NOW - timedelta(days=53)
        with patch("src.run_daily.cache_fetched_at", return_value=previous):
            report_cache_refresh(SlackNotifier(dry_run=True), "JPX銘柄マスター", "jpx_master.json", previous,
                                 dry_run=False, failures=failures)
        self.assertEqual(len(failures), 1)

    def test_stale_sources_and_missed_delivery_are_visible_to_independent_monitor(self):
        e = self.audited()
        e["eligibility"] = {"status": "eligible"}
        state = {"events": [e], "source_health": {"jpx_master": {"last_success_at": (NOW-timedelta(days=53)).isoformat()}}}
        self.assertIn("53日", stale_sources(state, NOW)[0])
        self.assertEqual(late_delivery_warnings(state, NOW.replace(day=19, hour=6, minute=14)), [])
        self.assertTrue(late_delivery_warnings(state, NOW.replace(day=19, hour=6, minute=15)))

    def test_master_accepts_the_current_official_xlsx_format(self):
        import openpyxl
        book = openpyxl.Workbook()
        book.active.append(["コード", "銘柄名", "市場・商品区分"])
        book.active.append(["648A", "テスト", "グロース（内国株式）"])
        content = BytesIO()
        book.save(content)
        self.assertEqual(parse_master_excel(content.getvalue()), {"648A": {"name": "テスト", "market": "グロース"}})


if __name__ == "__main__":
    unittest.main()
