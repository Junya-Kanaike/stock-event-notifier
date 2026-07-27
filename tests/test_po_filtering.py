from datetime import datetime
import unittest
from unittest.mock import patch

from src.collectors.tdnet import Disclosure
from src.core.bizday import JST
from src.notifiers.slack import SlackNotifier
from src.run_daily import drop_unsupported_po_events
from src.run_poll import handle_po


class PoFilteringTest(unittest.TestCase):
    def test_negotiated_sale_is_not_notified_or_saved_as_po(self):
        disclosure = Disclosure(
            id="140120260714592985",
            code="4071",
            name="プラスアルファ・コンサルティング",
            title="ラクスとの資本業務提携、当社株式の売出しに関するお知らせ",
            announced_at=datetime(2026, 7, 14, 15, 30, tzinfo=JST),
            pdf_url="https://example.test/4071.pdf",
        )
        text = """
        売出価格 1株につき3,350円
        売出価格については、売買当事者における協議の上、決定されております。
        受渡期日 2026年8月21日
        """
        state = {"notified_ids": [], "events": []}
        notifier = SlackNotifier(dry_run=True)

        with patch("src.run_poll.fetch_pdf_text", return_value=text):
            changed = handle_po(disclosure, state, notifier, {}, {})

        self.assertTrue(changed)
        self.assertEqual(notifier.sent_messages, [])
        self.assertEqual(state["events"], [])

    def test_market_priced_secondary_offering_is_still_notified_and_saved(self):
        disclosure = Disclosure(
            id="valid-po",
            code="205A",
            name="ロゴスホールディングス",
            title="株式の売出し並びに主要株主の異動に関するお知らせ",
            announced_at=datetime(2026, 7, 27, 16, 0, tzinfo=JST),
            pdf_url="https://example.test/205A.pdf",
        )
        text = """
        売出価格等決定日 2026年8月3日から2026年8月5日まで
        売出株式数 1,000,000株
        受渡期日 2026年8月12日
        """
        state = {"notified_ids": [], "events": []}
        notifier = SlackNotifier(dry_run=True)

        with patch("src.run_poll.fetch_pdf_text", return_value=text):
            changed = handle_po(disclosure, state, notifier, {}, {})

        self.assertTrue(changed)
        self.assertEqual(len(notifier.sent_messages), 1)
        self.assertEqual([event["id"] for event in state["events"]], ["po-205A-2026-07-27"])

    def test_legacy_false_positive_events_are_removed_before_daily_notifications(self):
        valid_event = {
            "id": "po-valid",
            "type": "po",
            "source_title": "株式の売出しに関するお知らせ",
            "detail": {"pricing_date_raw": "価格決定日 2026年8月3日"},
        }
        state = {
            "events": [
                valid_event,
                {
                    "id": "po-bond",
                    "type": "po",
                    "source_title": "公募ハイブリッド社債の発行条件決定について",
                    "detail": {},
                },
                {
                    "id": "po-negotiated",
                    "type": "po",
                    "source_title": "当社株式の売出しに関するお知らせ",
                    "detail": {
                        "pricing_date_raw": "売出価格は売買当事者における協議の上、決定されております"
                    },
                },
            ]
        }

        self.assertTrue(drop_unsupported_po_events(state))
        self.assertEqual(state["events"], [valid_event])
        self.assertFalse(drop_unsupported_po_events(state))


if __name__ == "__main__":
    unittest.main()
