from datetime import date
import unittest

from src.core.scheduler import build_bunbai_schedule, build_po_schedule, due_notifications
from src.notifiers.slack import SlackNotifier
import src.run_daily as run_daily


class SchedulerTest(unittest.TestCase):
    def test_po_schedule_uses_business_days(self):
        schedule = build_po_schedule(date(2026, 7, 17), date(2026, 7, 28))
        by_label = {item["label"]: item["date"] for item in schedule}
        self.assertEqual(by_label["pricing_day"], "2026-07-17")
        self.assertEqual(by_label["pricing_day+1"], "2026-07-21")
        self.assertEqual(by_label["pricing_day+2"], "2026-07-22")
        self.assertEqual(by_label["pricing_day+25bd"], "2026-08-25")
        self.assertEqual(by_label["pricing_day+26bd"], "2026-08-26")

    def test_bunbai_schedule(self):
        schedule = build_bunbai_schedule(date(2026, 7, 21))
        by_label = {item["label"]: item["date"] for item in schedule}
        self.assertEqual(by_label["execution-1bd"], "2026-07-17")
        self.assertEqual(by_label["execution+5bd"], "2026-07-28")

    def test_dummy_daily_notification_message_and_dry_run_send(self):
        state = {
            "notified_ids": [],
            "events": [
                {
                    "id": "po-7203-20260717",
                    "type": "po",
                    "code": "7203",
                    "name": "トヨタ自動車",
                    "market": "プライム",
                    "margin": "貸借",
                    "detail": {},
                    "schedule": [{"date": "2026-07-17", "label": "pricing_day", "sent": False}],
                }
            ],
        }
        due = due_notifications(state, date(2026, 7, 17))
        self.assertEqual(len(due), 1)
        self.assertIn("寄り付きで買う", due[0].text)
        notifier = SlackNotifier(dry_run=True)
        notifier.send(due[0].event["type"], due[0].text)
        self.assertEqual(len(notifier.sent_messages), 1)

    def test_daily_sync_preserves_sent_flags(self):
        original_fetch_ipos = run_daily.fetch_ipos
        try:
            run_daily.fetch_ipos = lambda: [{"code": "1234", "name": "テスト", "listing_date": "2026-07-21", "source_url": "https://example.test"}]
            state = {
                "notified_ids": [],
                "events": [
                    {
                        "id": "ipo-1234-2026-07-21",
                        "type": "ipo",
                        "code": "1234",
                        "name": "テスト",
                        "market": "グロース",
                        "margin": "対象外",
                        "announced_at": "2026-07-01T07:30:00+09:00",
                        "detail": {"listing_date": "2026-07-21"},
                        "pdf_url": "https://example.test",
                        "schedule": [
                            {"date": "2026-07-17", "label": "listing-1bd", "sent": True},
                            {"date": "2026-07-21", "label": "listing_day", "sent": False},
                        ],
                    }
                ],
            }
            changed = run_daily.sync_ipo_events(state, {"1234": {"name": "テスト", "market": "グロース"}}, {})
            self.assertFalse(changed)
            by_label = {item["label"]: item["sent"] for item in state["events"][0]["schedule"]}
            self.assertTrue(by_label["listing-1bd"])
            self.assertFalse(by_label["listing_day"])
        finally:
            run_daily.fetch_ipos = original_fetch_ipos


if __name__ == "__main__":
    unittest.main()
