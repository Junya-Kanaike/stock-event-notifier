import unittest

from src.core.reconcile import reconcile_event_state


class ReconcileStateTest(unittest.TestCase):
    def test_removes_known_po_and_cb_false_positives_and_reclassifies_split(self):
        state = {
            "events": [
                self.event("po-bond", "po", "9509", "公募ハイブリッド社債（劣後特約付社債）の発行について"),
                self.event("po-valid", "po", "7203", "公募による新株式発行及び株式売出しに関するお知らせ"),
                self.event("cb-adjust", "cb", "6383", "転換社債型新株予約権付社債の転換価額の調整に関するお知らせ"),
                self.event(
                    "cb-4062-2026-08-04",
                    "cb",
                    "4062",
                    "株式分割及び転換社債型新株予約権付社債の転換価額の調整に関するお知らせ",
                ),
            ]
        }

        self.assertTrue(reconcile_event_state(state))

        self.assertEqual(
            [event["id"] for event in state["events"]],
            ["po-valid", "split-4062-2026-08-04"],
        )
        recovered = state["events"][1]
        self.assertEqual(recovered["type"], "split")
        self.assertTrue(recovered["detail"]["recovery_needed"])

    def test_consolidates_bunbai_po_and_split_updates_and_preserves_sent_flags(self):
        bunbai_original = self.event("bunbai-1", "bunbai", "4073", "株式の立会外分売に関するお知らせ")
        bunbai_original["detail"] = {"execution_date": "2026-08-24", "execution_date_confirmed": False}
        bunbai_original["schedule"] = [{"date": "2026-08-24", "label": "execution_day", "sent": True}]
        bunbai_update = self.event("bunbai-2", "bunbai", "4073", "株式の立会外分売終了に関するお知らせ", "2026-08-24")
        bunbai_update["detail"] = {"execution_date": "2026-08-24", "execution_date_confirmed": True}
        bunbai_update["schedule"] = [{"date": "2026-08-24", "label": "execution_day", "sent": False}]

        po_original = self.event("po-1", "po", "7966", "株式の売出しに関するお知らせ")
        po_original["detail"] = {"size_oku": 300.0, "pricing_date_confirmed": False}
        po_update = self.event("po-2", "po", "7966", "売出株式数の変更に関するお知らせ", "2026-08-24")
        po_update["detail"] = {"size_oku": None, "pricing_date_confirmed": False}

        split_original = self.event("split-1", "split", "2108", "株式分割に関するお知らせ")
        split_original["detail"] = {"ratio": "3", "effective_date": "2026-10-01"}
        split_update = self.event("split-2", "split", "2108", "（開示事項の変更）株式分割に関するお知らせ", "2026-08-24")
        split_update["detail"] = {"ratio": None, "effective_date": None}

        state = {"events": [bunbai_original, bunbai_update, po_original, po_update, split_original, split_update]}

        self.assertTrue(reconcile_event_state(state))

        self.assertEqual(len(state["events"]), 3)
        bunbai = next(event for event in state["events"] if event["type"] == "bunbai")
        self.assertTrue(bunbai["detail"]["execution_date_confirmed"])
        self.assertTrue(next(item for item in bunbai["schedule"] if item["label"] == "execution_day")["sent"])
        po = next(event for event in state["events"] if event["type"] == "po")
        self.assertEqual(po["detail"]["size_oku"], 300.0)
        split = next(event for event in state["events"] if event["type"] == "split")
        self.assertEqual(split["detail"]["ratio"], "3")
        self.assertEqual(split["detail"]["effective_date"], "2026-10-01")
        self.assertFalse(reconcile_event_state(state))

    def test_marks_bad_split_ratio_for_recovery_and_clears_ambiguous_po_settlement(self):
        split = self.event("split-1", "split", "5706", "株式分割に関するお知らせ")
        split["detail"] = {"ratio": "1", "effective_date": "2026-10-01"}
        po = self.event("po-1", "po", "7966", "株式の売出しに関するお知らせ")
        po["detail"] = {
            "settlement_date": "2026-08-20",
            "settlement_date_raw": "受渡期日と同一とする。後段の日付 2026年8月20日",
            "settlement_date_status": "confirmed",
            "pricing_date_confirmed": False,
        }
        po["schedule"] = [{"date": "2026-08-20", "label": "settlement", "sent": False}]
        state = {"events": [split, po]}

        self.assertTrue(reconcile_event_state(state))

        self.assertIsNone(state["events"][0]["detail"]["ratio"])
        self.assertTrue(state["events"][0]["detail"]["recovery_needed"])
        po_event = state["events"][1]
        self.assertIsNone(po_event["detail"]["settlement_date"])
        self.assertEqual(po_event["schedule"], [])

    @staticmethod
    def event(event_id, event_type, code, title, day="2026-08-20"):
        return {
            "id": event_id,
            "type": event_type,
            "code": code,
            "announced_at": f"{day}T15:00:00+09:00",
            "source_title": title,
            "detail": {},
            "schedule": [],
            "related_disclosures": [{"id": event_id, "title": title}],
        }


if __name__ == "__main__":
    unittest.main()
