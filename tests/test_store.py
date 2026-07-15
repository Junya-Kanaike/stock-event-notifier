import unittest
from datetime import date
from pathlib import Path
import tempfile

from src.core.store import (
    add_notified_id,
    archive_completed_events,
    has_notified,
    record_source_result,
    trim_notified_ids,
    upsert_event,
)


class StoreTest(unittest.TestCase):
    def test_notified_id_dedupe(self):
        state = {"notified_ids": [], "events": []}
        self.assertFalse(has_notified(state, "202607070001"))
        self.assertTrue(add_notified_id(state, "202607070001"))
        self.assertTrue(has_notified(state, "202607070001"))
        self.assertFalse(add_notified_id(state, "202607070001"))
        self.assertEqual(state["notified_ids"], ["202607070001"])

    def test_upsert_event(self):
        state = {"notified_ids": [], "events": []}
        event = {"id": "po-1", "type": "po", "schedule": []}
        _, changed = upsert_event(state, event)
        self.assertTrue(changed)
        _, changed = upsert_event(state, event)
        self.assertFalse(changed)
        updated = {"id": "po-1", "type": "po", "schedule": [], "name": "updated"}
        _, changed = upsert_event(state, updated)
        self.assertTrue(changed)
        self.assertEqual(state["events"][0]["name"], "updated")

    def test_trim_notified_ids_keeps_newest(self):
        state = {"notified_ids": ["1", "2", "3"], "events": []}
        self.assertTrue(trim_notified_ids(state, limit=2))
        self.assertEqual(state["notified_ids"], ["2", "3"])

    def test_archive_completed_events(self):
        state = {
            "notified_ids": [],
            "events": [
                {
                    "id": "ipo-old",
                    "type": "ipo",
                    "schedule": [{"date": "2026-01-01", "label": "listing_day", "sent": True}],
                },
                {
                    "id": "po-active",
                    "type": "po",
                    "schedule": [{"date": "2026-08-01", "label": "pricing_day", "sent": False}],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            count = archive_completed_events(state, date(2026, 7, 15), archive_dir=Path(tmp))
            self.assertEqual(count, 1)
            self.assertEqual([event["id"] for event in state["events"]], ["po-active"])
            self.assertTrue((Path(tmp) / "events-2026.json").exists())

    def test_source_health_alerts_once_per_empty_day_after_threshold(self):
        state = {"notified_ids": [], "events": []}
        for day in (date(2026, 7, 13), date(2026, 7, 14)):
            changed, alert = record_source_result(state, "tdnet", day, 0)
            self.assertTrue(changed)
            self.assertFalse(alert)
        changed, alert = record_source_result(state, "tdnet", date(2026, 7, 15), 0)
        self.assertTrue(changed)
        self.assertTrue(alert)
        self.assertEqual(record_source_result(state, "tdnet", date(2026, 7, 15), 0), (False, False))
        changed, alert = record_source_result(state, "tdnet", date(2026, 7, 15), 4)
        self.assertTrue(changed)
        self.assertFalse(alert)
        self.assertEqual(state["source_health"]["tdnet"]["consecutive_empty_days"], 0)


if __name__ == "__main__":
    unittest.main()
