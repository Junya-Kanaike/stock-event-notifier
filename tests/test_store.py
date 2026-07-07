import unittest

from src.core.store import add_notified_id, has_notified, upsert_event


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


if __name__ == "__main__":
    unittest.main()
