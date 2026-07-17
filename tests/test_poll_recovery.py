from datetime import date, datetime
import unittest
from unittest.mock import patch

from src.collectors.tdnet import Disclosure
from src.core.bizday import JST
from src.notifiers.slack import SlackNotifier
from src.run_poll import handle_buyback, handle_po_correction, handle_po_pricing, original_disclosure_date


class PollRecoveryTest(unittest.TestCase):
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
