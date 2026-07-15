from datetime import datetime
import unittest
from unittest.mock import patch

from src.collectors.tdnet import Disclosure
from src.core.bizday import JST
from src.notifiers.slack import SlackNotifier
from src.run_poll import handle_buyback, handle_po_pricing


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


if __name__ == "__main__":
    unittest.main()
