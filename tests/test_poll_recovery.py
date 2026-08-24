from datetime import date, datetime
import unittest
from unittest.mock import patch

from src.collectors.tdnet import Disclosure
from src.core.bizday import JST
from src.notifiers.slack import SlackNotifier
from src.run_poll import (
    handle_bunbai,
    handle_buyback,
    handle_po,
    handle_po_correction,
    handle_po_pricing,
    handle_split,
    enrich_event_markets,
    fetch_poll_disclosures,
    original_disclosure_date,
    notify_unresolved_split_events,
    poll_target_dates,
    process_disclosure,
    process_disclosure_batch,
    recover_missing_event_markets,
    recover_split_events,
)


class PollRecoveryTest(unittest.TestCase):
    def test_default_poll_includes_previous_business_day(self):
        self.assertEqual(
            poll_target_dates(date(2026, 8, 24), explicit_date=False),
            [date(2026, 8, 21), date(2026, 8, 24)],
        )
        self.assertEqual(
            poll_target_dates(date(2026, 8, 24), explicit_date=True),
            [date(2026, 8, 24)],
        )

    def test_poll_fetch_continues_when_one_date_fails(self):
        disclosure = Disclosure(
            id="split-1",
            code="7203",
            name="テスト",
            title="株式分割に関するお知らせ",
            announced_at=datetime(2026, 8, 24, 15, 0, tzinfo=JST),
        )
        notifier = SlackNotifier(dry_run=True)
        with patch("src.run_poll.fetch_disclosures", side_effect=[RuntimeError("temporary"), [disclosure]]):
            disclosures, failures = fetch_poll_disclosures(
                [date(2026, 8, 21), date(2026, 8, 24)], notifier
            )

        self.assertEqual(disclosures, [disclosure])
        self.assertEqual(len(failures), 1)
        self.assertIn("2026-08-21", failures[0])

    def test_disclosure_failure_does_not_stop_later_items_and_abandons_after_five(self):
        first = Disclosure(
            id="bad-1",
            code="1111",
            name="失敗",
            title="株式分割に関するお知らせ",
            announced_at=datetime(2026, 8, 24, 15, 0, tzinfo=JST),
        )
        second = Disclosure(
            id="good-1",
            code="2222",
            name="成功",
            title="株式分割に関するお知らせ",
            announced_at=datetime(2026, 8, 24, 15, 1, tzinfo=JST),
        )
        state = {"events": [], "notified_ids": []}
        notifier = SlackNotifier(dry_run=True)

        with patch("src.run_poll.process_disclosure", side_effect=[RuntimeError("bad"), True]):
            changed = process_disclosure_batch(
                [first, second], state, notifier, {}, {}, dry_run=True
            )

        self.assertTrue(changed)
        self.assertNotIn("bad-1", state["notified_ids"])
        self.assertIn("good-1", state["notified_ids"])
        self.assertEqual(state["disclosure_failures"]["bad-1"]["count"], 1)

        for _ in range(4):
            with patch("src.run_poll.process_disclosure", side_effect=RuntimeError("bad")):
                process_disclosure_batch([first], state, notifier, {}, {}, dry_run=True)
        self.assertIn("bad-1", state["notified_ids"])
        self.assertTrue(state["disclosure_failures"]["bad-1"]["abandoned"])

    def test_pdf_failure_keeps_po_notification_flow_alive(self):
        disclosure = Disclosure(
            id="po-pdf-failure",
            code="7203",
            name="テスト",
            title="公募による新株式発行に関するお知らせ",
            announced_at=datetime(2026, 8, 24, 15, 0, tzinfo=JST),
            pdf_url="https://example.test/missing.pdf",
        )
        state = {"events": []}
        notifier = SlackNotifier(dry_run=True)

        with patch("src.run_poll.fetch_pdf_text", side_effect=RuntimeError("404")):
            changed = handle_po(disclosure, state, notifier, {}, {})

        self.assertTrue(changed)
        self.assertEqual(len(state["events"]), 1)
        self.assertIn("PDF取得失敗", state["events"][0]["detail"]["parse_warnings"][0])
        self.assertTrue(any(item["type"] == "po" for item in notifier.sent_messages))
        self.assertTrue(any(item["type"] == "system" for item in notifier.sent_messages))

    def test_source_market_fills_master_gap_and_existing_event(self):
        disclosure = Disclosure(
            id="regional-1",
            code="5075",
            name="アップコン",
            title="株式の立会外分売に関するお知らせ",
            announced_at=datetime(2026, 8, 24, 15, 0, tzinfo=JST),
            market="名証",
        )
        event = {
            "id": "bunbai-5075",
            "type": "bunbai",
            "code": "5075",
            "market": "取得失敗",
            "related_disclosures": [{"id": "regional-1"}],
        }
        state = {"events": [event]}

        self.assertTrue(enrich_event_markets(state, [disclosure]))
        self.assertEqual(event["market"], "名証")

    def test_missing_market_is_recovered_from_original_disclosure_date(self):
        disclosure = Disclosure(
            id="regional-old",
            code="5075",
            name="アップコン",
            title="株式の立会外分売に関するお知らせ",
            announced_at=datetime(2026, 7, 21, 15, 0, tzinfo=JST),
            market="名証",
        )
        state = {
            "events": [
                {
                    "id": "bunbai-5075",
                    "type": "bunbai",
                    "market": "取得失敗",
                    "related_disclosures": [
                        {
                            "id": "regional-old",
                            "announced_at": "2026-07-21T15:00:00+09:00",
                        }
                    ],
                }
            ]
        }

        with patch("src.run_poll.fetch_disclosures", return_value=[disclosure]) as fetch:
            changed = recover_missing_event_markets(state, [])

        self.assertTrue(changed)
        self.assertEqual(state["events"][0]["market"], "名証")
        fetch.assert_called_once_with(date(2026, 7, 21))

    def test_bunbai_followup_updates_original_event(self):
        disclosure = Disclosure(
            id="bunbai-update",
            code="4073",
            name="テスト",
            title="株式の立会外分売実施に関するお知らせ",
            announced_at=datetime(2026, 8, 21, 16, 35, tzinfo=JST),
            pdf_url="https://example.test/update.pdf",
        )
        state = {
            "events": [
                {
                    "id": "bunbai-4073-2026-08-14",
                    "type": "bunbai",
                    "code": "4073",
                    "name": "テスト",
                    "market": "グロース",
                    "margin": "信用",
                    "announced_at": "2026-08-14T15:30:00+09:00",
                    "detail": {"execution_date": "2026-08-24", "execution_date_confirmed": False},
                    "schedule": [{"date": "2026-08-24", "label": "execution_day", "sent": True}],
                    "pdf_url": "https://example.test/original.pdf",
                    "related_disclosures": [{"id": "bunbai-original", "relation": "source"}],
                }
            ]
        }
        notifier = SlackNotifier(dry_run=True)

        with patch("src.run_poll.fetch_pdf_text", return_value="分売実施日 2026年8月24日"):
            handle_bunbai(disclosure, state, notifier, {}, {})

        self.assertEqual(len(state["events"]), 1)
        event = state["events"][0]
        self.assertEqual(event["id"], "bunbai-4073-2026-08-14")
        self.assertTrue(event["detail"]["execution_date_confirmed"])
        self.assertTrue(next(item for item in event["schedule"] if item["label"] == "execution_day")["sent"])
        self.assertEqual(event["related_disclosures"][-1]["relation"], "execution")

    def test_split_change_updates_original_event_without_erasing_details(self):
        disclosure = Disclosure(
            id="split-update",
            code="2108",
            name="テスト",
            title="（開示事項の変更）株式分割により増加する株式数の変更に関するお知らせ",
            announced_at=datetime(2026, 8, 24, 10, 30, tzinfo=JST),
            pdf_url="https://example.test/update.pdf",
        )
        state = {
            "events": [
                {
                    "id": "split-2108-2026-08-21",
                    "type": "split",
                    "code": "2108",
                    "announced_at": "2026-08-21T15:00:00+09:00",
                    "detail": {"ratio": "3", "effective_date": "2026-10-01"},
                    "schedule": [{"date": "2026-10-01", "label": "effective_day", "sent": False}],
                    "related_disclosures": [{"id": "split-original", "relation": "source"}],
                }
            ]
        }

        with patch("src.run_poll.fetch_pdf_text", return_value="変更後の発行済株式数のみを記載"):
            changed = handle_split(disclosure, state, SlackNotifier(dry_run=True), {}, {})

        self.assertTrue(changed)
        self.assertEqual(len(state["events"]), 1)
        event = state["events"][0]
        self.assertEqual(event["detail"]["ratio"], "3")
        self.assertEqual(event["detail"]["effective_date"], "2026-10-01")
        self.assertEqual(event["related_disclosures"][-1]["relation"], "correction")

    def test_split_without_effective_date_sends_review_notification(self):
        disclosure = Disclosure(
            id="split-missing-date",
            code="7203",
            name="テスト",
            title="株式分割に関するお知らせ",
            announced_at=datetime(2026, 8, 24, 15, 0, tzinfo=JST),
            pdf_url="https://example.test/split.pdf",
        )
        notifier = SlackNotifier(dry_run=True)
        state = {"events": []}

        with patch("src.run_poll.fetch_pdf_text", return_value="普通株式1株を2株に分割"):
            changed = handle_split(disclosure, state, notifier, {}, {})

        self.assertTrue(changed)
        self.assertTrue(any(item["type"] == "split" for item in notifier.sent_messages))
        self.assertTrue(any(item["type"] == "system" for item in notifier.sent_messages))
        self.assertTrue(state["events"][0]["detail"]["review_notified"])

    def test_existing_split_without_date_gets_one_recovery_review(self):
        state = {
            "events": [
                {
                    "id": "split-old",
                    "type": "split",
                    "code": "249A",
                    "name": "テスト",
                    "market": "グロース",
                    "detail": {"ratio": None, "effective_date": None},
                    "schedule": [],
                }
            ]
        }
        notifier = SlackNotifier(dry_run=True)

        self.assertTrue(notify_unresolved_split_events(state, notifier))
        self.assertFalse(notify_unresolved_split_events(state, notifier))
        self.assertEqual(
            len([item for item in notifier.sent_messages if item["type"] == "split"]),
            1,
        )

    def test_recovery_reparses_bad_integer_split_ratio(self):
        state = {
            "events": [
                {
                    "id": "split-5706-2026-08-07",
                    "type": "split",
                    "code": "5706",
                    "announced_at": "2026-08-07T15:30:00+09:00",
                    "detail": {"ratio": None, "effective_date": "2026-10-01", "recovery_needed": True},
                    "schedule": [],
                    "pdf_url": "https://example.test/split.pdf",
                }
            ]
        }

        with patch(
            "src.run_poll.fetch_pdf_text",
            return_value="普通株式1株を10株に分割します。効力発生日 2026年10月1日",
        ):
            changed = recover_split_events(state, SlackNotifier(dry_run=True))

        self.assertTrue(changed)
        detail = state["events"][0]["detail"]
        self.assertEqual(detail["ratio"], "10")
        self.assertNotIn("recovery_needed", detail)
        self.assertEqual(len(state["events"][0]["schedule"]), 2)

    def test_multi_class_disclosure_processes_each_independent_event(self):
        disclosure = Disclosure(
            id="multi-1",
            code="7203",
            name="テスト",
            title="複合開示",
            announced_at=datetime(2026, 8, 25, 15, 0, tzinfo=JST),
        )
        with patch("src.run_poll.handle_cb", return_value=True) as cb, patch(
            "src.run_poll.handle_split", return_value=True
        ) as split:
            changed = process_disclosure(
                disclosure,
                {"cb", "split"},
                {"events": []},
                SlackNotifier(dry_run=True),
                {},
                {"7203": "貸借"},
                set(),
            )

        self.assertTrue(changed)
        cb.assert_called_once()
        split.assert_called_once()

    def test_pricing_disclosure_recovers_missing_po_event(self):
        disclosure = Disclosure(
            id="pricing-1",
            code="7203",
            name="テスト",
            title="発行価格等の決定に関するお知らせ",
            announced_at=datetime(2026, 7, 15, 16, 0, tzinfo=JST),
            pdf_url="https://example.test/pricing.pdf",
        )
        state = {"notified_ids": [], "events": []}
        notifier = SlackNotifier(dry_run=True)
        with patch("src.run_poll.fetch_pdf_text", return_value="受渡期日 2026年7月24日"):
            changed = handle_po_pricing(
                disclosure,
                state,
                notifier,
                {"7203": {"name": "テスト", "market": "プライム"}},
                {"7203": "貸借"},
            )
        self.assertTrue(changed)
        self.assertEqual(len(state["events"]), 1)
        self.assertTrue(state["events"][0]["detail"]["pricing_date_confirmed"])
        self.assertEqual(state["events"][0]["detail"]["pricing_date"], "2026-07-15")
        self.assertEqual(len(notifier.sent_messages), 1)

    def test_buyback_state_is_not_canceled_when_correction_send_fails(self):
        class FailingNotifier:
            def send(self, *args, **kwargs):
                raise RuntimeError("send failed")

        disclosure = Disclosure(
            id="buyback-1",
            code="7203",
            name="テスト",
            title="自己株式の取得に関するお知らせ",
            announced_at=datetime(2026, 7, 15, 17, 0, tzinfo=JST),
        )
        state = {
            "notified_ids": [],
            "events": [
                {
                    "id": "cb-7203-2026-07-15",
                    "type": "cb",
                    "code": "7203",
                    "name": "テスト",
                    "announced_at": "2026-07-15T16:00:00+09:00",
                    "detail": {"canceled": False},
                    "schedule": [],
                }
            ],
        }
        with self.assertRaises(RuntimeError):
            handle_buyback(disclosure, state, FailingNotifier())
        self.assertFalse(state["events"][0]["detail"]["canceled"])

    def test_pricing_disclosure_enriches_existing_po_and_sends_details(self):
        disclosure = Disclosure(
            id="pricing-2",
            code="7203",
            name="テスト",
            title="発行価格及び売出価格等の決定に関するお知らせ",
            announced_at=datetime(2026, 7, 16, 16, 0, tzinfo=JST),
            pdf_url="https://example.test/pricing.pdf",
        )
        state = {
            "events": [
                {
                    "id": "po-7203-2026-07-10",
                    "type": "po",
                    "code": "7203",
                    "name": "テスト",
                    "market": "プライム",
                    "margin": "貸借",
                    "announced_at": "2026-07-10T15:00:00+09:00",
                    "detail": {"po_kind": "both", "pricing_date_confirmed": False},
                    "schedule": [],
                    "pdf_url": "https://example.test/original.pdf",
                }
            ]
        }
        notifier = SlackNotifier(dry_run=True)
        pdf_text = """
        払込金額（発行価額）の総額 10,000百万円
        売出価額の総額 2,000百万円
        受渡期日 2026年7月24日
        """

        with patch("src.run_poll.fetch_pdf_text", return_value=pdf_text):
            handle_po_pricing(disclosure, state, notifier, {}, {})

        event = state["events"][0]
        self.assertEqual(event["detail"]["size_oku"], 120.0)
        self.assertEqual(event["detail"]["size_status"], "confirmed")
        self.assertTrue(event["detail"]["pricing_date_confirmed"])
        self.assertEqual(event["latest_pdf_url"], disclosure.pdf_url)
        self.assertIn("吸収規模: 約120億円（確定）", notifier.sent_messages[0]["payload"]["text"])

    def test_correction_merges_into_original_event_without_erasing_known_values(self):
        disclosure = Disclosure(
            id="correction-1",
            code="7203",
            name="テスト",
            title="（訂正）株式の売出しに関するお知らせの一部訂正",
            announced_at=datetime(2026, 7, 16, 17, 0, tzinfo=JST),
            pdf_url="https://example.test/correction.pdf",
        )
        state = {
            "events": [
                {
                    "id": "po-7203-2026-07-10",
                    "type": "po",
                    "code": "7203",
                    "name": "テスト",
                    "market": "プライム",
                    "margin": "貸借",
                    "announced_at": "2026-07-10T15:00:00+09:00",
                    "detail": {
                        "po_kind": "secondary",
                        "size_oku": 100.0,
                        "size_status": "confirmed",
                        "pricing_date": "2026-07-20",
                        "pricing_date_confirmed": False,
                    },
                    "schedule": [],
                    "pdf_url": "https://example.test/original.pdf",
                }
            ]
        }
        notifier = SlackNotifier(dry_run=True)

        with patch("src.run_poll.fetch_pdf_text", return_value="受渡期日 2026年7月28日"):
            handle_po_correction(disclosure, state, notifier, {}, {})

        event = state["events"][0]
        self.assertEqual(len(state["events"]), 1)
        self.assertEqual(event["detail"]["size_oku"], 100.0)
        self.assertEqual(event["detail"]["settlement_date"], "2026-07-28")
        self.assertEqual(event["latest_pdf_url"], disclosure.pdf_url)
        self.assertEqual(event["related_disclosures"][-1]["relation"], "correction")

    def test_correction_recovers_original_disclosure_from_referenced_date(self):
        correction = Disclosure(
            id="correction-2",
            code="4071",
            name="訂正会社",
            title="（訂正）当社株式の売出しに関するお知らせの一部訂正",
            announced_at=datetime(2026, 7, 15, 17, 0, tzinfo=JST),
            pdf_url="https://example.test/correction.pdf",
        )
        original = Disclosure(
            id="original-1",
            code="4071",
            name="訂正会社",
            title="当社株式の売出しに関するお知らせ",
            announced_at=datetime(2026, 7, 14, 15, 0, tzinfo=JST),
            pdf_url="https://example.test/original.pdf",
        )
        correction_text = "2026 年 7 月 14 日に開示いたしました資料の記載を訂正します。"
        original_text = "売出価額の総額 5,000百万円 価格決定日 2026年7月20日 受渡期日 2026年7月28日"
        state = {"events": []}
        notifier = SlackNotifier(dry_run=True)

        with patch("src.run_poll.fetch_disclosures", return_value=[original]) as fetch_list, patch(
            "src.run_poll.fetch_pdf_text", side_effect=[correction_text, original_text]
        ):
            handle_po_correction(correction, state, notifier, {}, {})

        fetch_list.assert_called_once_with(date(2026, 7, 14))
        event = state["events"][0]
        self.assertEqual(event["id"], "po-4071-2026-07-14")
        self.assertEqual(event["detail"]["size_oku"], 50.0)
        self.assertEqual(event["detail"]["recovery_notes"], ["訂正資料から元開示を自動補完"])
        self.assertEqual([item["relation"] for item in event["related_disclosures"]], ["original", "correction"])

    def test_original_disclosure_date_uses_date_before_correction_marker(self):
        text = "2026 年 7 月 14 日に開示いたしました資料を訂正します。"
        self.assertEqual(original_disclosure_date(text, 2026), date(2026, 7, 14))


if __name__ == "__main__":
    unittest.main()
