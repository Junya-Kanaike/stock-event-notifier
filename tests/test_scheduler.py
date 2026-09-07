from datetime import date, datetime
import unittest
from unittest.mock import Mock

from src.core.bizday import JST
from src.core.scheduler import build_bunbai_schedule, build_po_schedule, build_split_schedule, due_notifications
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

    def test_split_schedule_uses_rights_final_date_and_time_gates(self):
        schedule = build_split_schedule(date(2026, 9, 25))
        by_label = {item["label"]: item for item in schedule}
        self.assertEqual(set(by_label), {"rights_final+0_after_close", "rights_final+1_noon", "rights_final+5_preopen"})
        self.assertEqual(by_label["rights_final+0_after_close"]["not_before_jst"], "19:00")
        self.assertEqual(by_label["rights_final+1_noon"]["date"], "2026-09-28")
        self.assertEqual(by_label["rights_final+5_preopen"]["date"], "2026-10-02")

    def test_split_time_boundaries_and_delayed_reference_text(self):
        event = {
            "id": "split-1",
            "type": "split",
            "code": "7203",
            "name": "テスト",
            "market": "プライム",
            "eligibility": {"status": "eligible", "reasons": []},
            "detail": {"ratio": "2"},
            "schedule": build_split_schedule(date(2026, 9, 25)),
        }
        state = {"events": [event]}
        self.assertEqual(due_notifications(state, datetime(2026, 9, 25, 18, 59, tzinfo=JST)), [])
        at_close = due_notifications(state, datetime(2026, 9, 25, 19, 0, tzinfo=JST))[0].text
        self.assertIn("分割の空売り検討", at_close)

        event["schedule"][0]["sent"] = True
        self.assertEqual(due_notifications(state, datetime(2026, 9, 28, 11, 59, tzinfo=JST)), [])
        noon = due_notifications(state, datetime(2026, 9, 28, 12, 0, tzinfo=JST))[0].text
        after_close = due_notifications(state, datetime(2026, 9, 28, 15, 30, tzinfo=JST))[0].text
        self.assertIn("大引で空売り", noon)
        self.assertNotIn("大引で空売り", after_close)

        event["schedule"][1]["sent"] = True
        self.assertEqual(due_notifications(state, datetime(2026, 10, 2, 7, 59, tzinfo=JST)), [])
        preopen = due_notifications(state, datetime(2026, 10, 2, 8, 0, tzinfo=JST))[0].text
        late = due_notifications(state, datetime(2026, 10, 2, 9, 0, tzinfo=JST))[0].text
        self.assertIn("寄り付きで買い戻し", preopen)
        self.assertNotIn("寄り付きで買い戻し", late)
        self.assertIn("参考通知", late)

    def test_schedule_date_change_preserves_already_sent_labels(self):
        old = build_po_schedule(date(2026, 9, 7), date(2026, 9, 15))
        next(item for item in old if item["label"] == "pricing_day")["sent"] = True
        rebuilt = build_po_schedule(date(2026, 9, 8), date(2026, 9, 16), old_schedule=old)
        self.assertTrue(next(item for item in rebuilt if item["label"] == "pricing_day")["sent"])

    def test_po_25_and_26_business_days_use_identical_action(self):
        event = {"type": "po", "code": "7203", "name": "テスト", "detail": {}}
        from src.core.scheduler import render_daily_message

        self.assertIn("寄り付きで半分売る", render_daily_message(event, "pricing_day+25bd"))
        self.assertIn("寄り付きで半分売る", render_daily_message(event, "pricing_day+26bd"))

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
                    "detail": {"pricing_date_confirmed": True},
                    "schedule": [{"date": "2026-07-17", "label": "pricing_day", "sent": False}],
                }
            ],
        }
        due = due_notifications(state, date(2026, 7, 17))
        self.assertEqual(len(due), 1)
        self.assertIn("寄り付きで買う", due[0].text)
        self.assertIn("吸収規模:", due[0].text)
        self.assertIn("希薄化率:", due[0].text)
        notifier = SlackNotifier(dry_run=True)
        notifier.send(due[0].event["type"], due[0].text)
        self.assertEqual(len(notifier.sent_messages), 1)

    def test_daily_notification_recovers_unsent_overdue_item(self):
        state = {
            "notified_ids": [],
            "events": [
                {
                    "id": "ipo-1234-2026-07-16",
                    "type": "ipo",
                    "code": "1234",
                    "name": "テスト",
                    "detail": {},
                    "schedule": [
                        {"date": "2026-07-16", "label": "listing_day", "sent": False},
                        {"date": "2026-07-18", "label": "listing_day", "sent": False},
                    ],
                }
            ],
        }

        due = due_notifications(state, date(2026, 7, 17))

        self.assertEqual(len(due), 1)
        self.assertTrue(due[0].overdue)
        self.assertEqual(due[0].scheduled_for, date(2026, 7, 16))
        self.assertIn("[遅延通知]", due[0].text)
        self.assertIn("2026-07-16", due[0].text)
        self.assertIn("現在時点の指示ではありません", due[0].text)

    def test_unconfirmed_po_uses_first_date_as_action_schedule(self):
        event = {
            "id": "po-6232-2026-08-24",
            "type": "po",
            "code": "6232",
            "name": "ＡＣＳＬ",
            "detail": {
                "pricing_date": "2026-08-25",
                "pricing_date_end": "2026-08-26",
                "pricing_date_confirmed": False,
            },
            "schedule": [
                {"date": "2026-08-25", "label": "pricing_day", "sent": False},
                {"date": "2026-08-26", "label": "pricing_day+1", "sent": False},
            ],
        }
        state = {"events": [event]}

        due = due_notifications(state, date(2026, 8, 25))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].schedule_item["label"], "pricing_day")

    def test_daily_notification_does_not_repeat_sent_overdue_item(self):
        state = {
            "events": [
                {
                    "id": "ipo-1234-2026-07-16",
                    "type": "ipo",
                    "schedule": [{"date": "2026-07-16", "label": "listing_day", "sent": True}],
                }
            ]
        }

        self.assertEqual(due_notifications(state, date(2026, 7, 17)), [])

    def test_po_daily_message_includes_saved_detail_and_status(self):
        state = {
            "events": [
                {
                    "id": "po-1234-2026-07-16",
                    "type": "po",
                    "code": "1234",
                    "name": "テスト",
                    "market": "プライム",
                    "margin": "貸借",
                    "detail": {
                        "po_kind": "secondary",
                        "size_oku": None,
                        "size_oku_min": 100.0,
                        "size_oku_max": 120.0,
                        "size_status": "estimated",
                        "size_basis": "株数（OA上限込み）×仮条件",
                        "dilution_pct": None,
                        "dilution_status": "unavailable",
                        "pricing_date": "2026-07-16",
                        "pricing_date_end": "2026-07-18",
                        "pricing_date_confirmed": True,
                        "pricing_date_status": "provisional",
                        "settlement_date": "2026-07-24",
                        "settlement_date_status": "confirmed",
                    },
                    "schedule": [{"date": "2026-07-16", "label": "pricing_day", "sent": False}],
                }
            ]
        }

        text = due_notifications(state, date(2026, 7, 16))[0].text

        self.assertIn("約100〜120億円（概算・株数（OA上限込み）×仮条件）", text)
        self.assertIn("希薄化率: 未取得", text)
        self.assertIn("2026-07-16〜2026-07-18（暫定）", text)

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
            changed = run_daily.sync_ipo_events(
                state,
                {"1234": {"name": "テスト", "market": "グロース"}},
                {"9999": "貸借"},
                as_of=date(2026, 7, 17),
            )
            self.assertTrue(changed)
            by_label = {item["label"]: item["sent"] for item in state["events"][0]["schedule"]}
            self.assertTrue(by_label["listing-1bd"])
            self.assertFalse(by_label["listing_day"])
            self.assertFalse(
                run_daily.sync_ipo_events(
                    state,
                    {"1234": {"name": "テスト", "market": "グロース"}},
                    {"9999": "貸借"},
                    as_of=date(2026, 7, 17),
                )
            )
        finally:
            run_daily.fetch_ipos = original_fetch_ipos

    def test_ipo_pending_then_target_confirmed_notification(self):
        original_fetch_ipos = run_daily.fetch_ipos
        try:
            records = [{"code": "603A", "name": "テスト", "listing_date": None, "market": "グロース"}]
            run_daily.fetch_ipos = lambda: records
            state = {"events": []}
            notifier = SlackNotifier(dry_run=True)
            run_daily.sync_ipo_events(
                state,
                {"603A": {"name": "テスト", "market": "グロース"}},
                {},
                as_of=date(2026, 9, 8),
                notifier=notifier,
            )
            self.assertIn("[IPO判定待ち]", notifier.sent_messages[0]["payload"]["text"])

            records[0]["listing_date"] = "2026-09-30"
            run_daily.sync_ipo_events(
                state,
                {"603A": {"name": "テスト", "market": "グロース"}},
                {},
                as_of=date(2026, 9, 8),
                notifier=notifier,
            )
            self.assertIn("[対象確定]", notifier.sent_messages[1]["payload"]["text"])
            self.assertEqual(len(state["events"][0]["schedule"]), 2)
        finally:
            run_daily.fetch_ipos = original_fetch_ipos

    def test_daily_system_summary_lists_required_unresolved_breakdowns_once(self):
        state = {
            "events": [
                {
                    "type": "po",
                    "code": "7203",
                    "eligibility": {"status": "pending", "reasons": ["吸収規模未確定"]},
                    "detail": {},
                    "schedule": [],
                },
                {
                    "type": "split",
                    "code": "6758",
                    "eligibility": {"status": "pending", "reasons": ["権利付最終日未取得"]},
                    "detail": {},
                    "schedule": [],
                },
            ],
            "source_health": {"tdnet": {"last_success_at": "2026-09-08T12:00:00+09:00"}},
        }
        notifier = SlackNotifier(dry_run=True)
        current = datetime(2026, 9, 8, 19, 30, tzinfo=JST)
        self.assertTrue(run_daily.send_daily_system_summary(state, notifier, current, failures=[]))
        text = notifier.sent_messages[0]["payload"]["text"]
        self.assertIn("イベント別未解決", text)
        self.assertIn("PO未取得: 7203(株価,株数,価格決定日)", text)
        self.assertIn("株式分割未確定: 6758(基準日,権利付最終日)", text)
        self.assertFalse(run_daily.send_daily_system_summary(state, notifier, current, failures=[]))

    def test_daily_sync_keeps_past_unsent_ipo_for_delayed_notification(self):
        original_fetch_ipos = run_daily.fetch_ipos
        try:
            run_daily.fetch_ipos = lambda: [
                {"code": "598A", "name": "過去", "market": "グロース", "listing_date": "2026-07-15"},
                {"code": "603A", "name": "A社", "market": "グロース", "listing_date": "2026-07-29"},
                {"code": "604A", "name": "B社", "market": "スタンダード", "listing_date": "2026-07-29"},
            ]
            state = {
                "notified_ids": [],
                "events": [
                    {
                        "id": "ipo-598A-2026-07-15",
                        "type": "ipo",
                        "code": "598A",
                        "detail": {"listing_date": "2026-07-15"},
                        "schedule": [{"date": "2026-07-15", "label": "listing_day", "sent": False}],
                    }
                ],
            }
            changed = run_daily.sync_ipo_events(state, {}, {"9999": "貸借"}, as_of=date(2026, 7, 16))
            self.assertTrue(changed)
            self.assertEqual({event["code"] for event in state["events"]}, {"598A", "603A", "604A"})
            due = due_notifications(state, date(2026, 7, 16))
            self.assertEqual([item.event["code"] for item in due], ["598A"])
        finally:
            run_daily.fetch_ipos = original_fetch_ipos

    def test_daily_sync_preserves_future_ipo_missing_from_temporary_feed(self):
        original_fetch_ipos = run_daily.fetch_ipos
        try:
            run_daily.fetch_ipos = lambda: []
            state = {
                "notified_ids": [],
                "events": [
                    {
                        "id": "ipo-603A-2026-07-29",
                        "type": "ipo",
                        "detail": {"listing_date": "2026-07-29"},
                        "schedule": [],
                    }
                ],
            }
            changed = run_daily.sync_ipo_events(state, {}, {}, as_of=date(2026, 7, 16))
            self.assertFalse(changed)
            self.assertEqual([event["id"] for event in state["events"]], ["ipo-603A-2026-07-29"])
        finally:
            run_daily.fetch_ipos = original_fetch_ipos

    def test_daily_sync_can_force_jpx_refresh(self):
        original_fetch_ipos = run_daily.fetch_ipos
        original_fetch_bunbai = run_daily.fetch_bunbai
        try:
            ipo_fetch = Mock(return_value=[])
            bunbai_fetch = Mock(return_value=[])
            run_daily.fetch_ipos = ipo_fetch
            run_daily.fetch_bunbai = bunbai_fetch

            state = {"notified_ids": [], "events": []}
            run_daily.sync_ipo_events(state, {}, {}, as_of=date(2026, 7, 16), force_refresh=True)
            run_daily.sync_bunbai_events(state, {}, {}, as_of=date(2026, 7, 16), force_refresh=True)

            ipo_fetch.assert_called_once_with(force=True)
            bunbai_fetch.assert_called_once_with(force=True)
        finally:
            run_daily.fetch_ipos = original_fetch_ipos
            run_daily.fetch_bunbai = original_fetch_bunbai

    def test_bunbai_sync_merges_pending_tdnet_event(self):
        original_fetch_bunbai = run_daily.fetch_bunbai
        try:
            run_daily.fetch_bunbai = lambda: [
                {"code": "7203", "name": "テスト", "execution_date": "2026-07-21", "source_url": "https://example.test"}
            ]
            state = {
                "notified_ids": [],
                "events": [
                    {
                        "id": "bunbai-7203-2026-07-15",
                        "type": "bunbai",
                        "code": "7203",
                        "announced_at": "2026-07-15T15:00:00+09:00",
                        "detail": {"execution_date": None, "execution_date_confirmed": False},
                        "schedule": [],
                    }
                ],
            }
            changed = run_daily.sync_bunbai_events(state, {}, {"9999": "貸借"}, as_of=date(2026, 7, 17))
            self.assertTrue(changed)
            self.assertEqual(len(state["events"]), 1)
            self.assertEqual(state["events"][0]["id"], "bunbai-7203-2026-07-15")
            self.assertTrue(state["events"][0]["detail"]["execution_date_confirmed"])
        finally:
            run_daily.fetch_bunbai = original_fetch_bunbai

    def test_bunbai_sync_reuses_existing_event_with_same_execution_date(self):
        original_fetch_bunbai = run_daily.fetch_bunbai
        try:
            run_daily.fetch_bunbai = lambda: [
                {"code": "4073", "name": "テスト", "execution_date": "2026-08-24", "source_url": "https://jpx.test"}
            ]
            state = {
                "events": [
                    {
                        "id": "bunbai-4073-2026-08-14",
                        "type": "bunbai",
                        "code": "4073",
                        "name": "テスト",
                        "announced_at": "2026-08-14T15:30:00+09:00",
                        "detail": {"execution_date": "2026-08-24", "execution_date_confirmed": True},
                        "schedule": [{"date": "2026-08-24", "label": "execution_day", "sent": True}],
                        "pdf_url": "https://tdnet.test/original.pdf",
                    }
                ]
            }

            changed = run_daily.sync_bunbai_events(
                state,
                {"4073": {"name": "テスト", "market": "グロース"}},
                {"4073": "信用"},
                as_of=date(2026, 8, 24),
            )

            self.assertTrue(changed)
            self.assertEqual(len(state["events"]), 1)
            self.assertEqual(state["events"][0]["id"], "bunbai-4073-2026-08-14")
            self.assertEqual(state["events"][0]["pdf_url"], "https://tdnet.test/original.pdf")
            self.assertTrue(next(item for item in state["events"][0]["schedule"] if item["label"] == "execution_day")["sent"])
        finally:
            run_daily.fetch_bunbai = original_fetch_bunbai

    def test_bunbai_sync_does_not_replay_legacy_announcement(self):
        original_fetch_bunbai = run_daily.fetch_bunbai
        try:
            run_daily.fetch_bunbai = lambda: [
                {"code": "4073", "name": "テスト", "execution_date": "2026-09-10", "source_url": "https://jpx.test"}
            ]
            state = {
                "events": [
                    {
                        "id": "bunbai-4073-old",
                        "type": "bunbai",
                        "code": "4073",
                        "name": "テスト",
                        "market": "グロース",
                        "margin": "信用",
                        "announced_at": "2026-09-01T15:30:00+09:00",
                        "detail": {"execution_date": "2026-09-10", "execution_date_confirmed": True},
                        "schedule": [],
                    }
                ]
            }
            notifier = SlackNotifier(dry_run=True)
            run_daily.sync_bunbai_events(
                state,
                {"4073": {"name": "テスト", "market": "グロース"}},
                {"4073": "信用"},
                as_of=date(2026, 9, 8),
                notifier=notifier,
            )
            self.assertEqual(notifier.sent_messages, [])
            self.assertTrue(state["events"][0]["detail"]["eligible_notified"])
        finally:
            run_daily.fetch_bunbai = original_fetch_bunbai

    def test_action_wording_respects_time_cutoffs(self):
        po = {
            "id": "po-1",
            "type": "po",
            "code": "7203",
            "name": "テスト",
            "eligibility": {"status": "eligible", "reasons": []},
            "detail": {},
            "schedule": [{"date": "2026-08-25", "label": "pricing_day+25bd", "sent": False, "action_cutoff_jst": "09:00"}],
        }
        before = due_notifications({"events": [po]}, datetime(2026, 8, 25, 8, 0, tzinfo=JST))[0].text
        after = due_notifications({"events": [po]}, datetime(2026, 8, 25, 9, 0, tzinfo=JST))[0].text
        self.assertIn("寄り付きで半分売る", before)
        self.assertNotIn("半分売る", after)
        self.assertIn("参考通知", after)

    def test_bunbai_previous_day_has_application_reminder(self):
        event = {
            "id": "bunbai-1",
            "type": "bunbai",
            "code": "4073",
            "name": "テスト",
            "schedule": [{"date": "2026-08-24", "label": "execution-1bd", "sent": False}],
        }
        text = due_notifications({"events": [event]}, date(2026, 8, 24))[0].text
        self.assertIn("申し込み忘れずに！", text)

    def test_split_time_gates_and_action_cutoffs(self):
        event = {
            "id": "split-1",
            "type": "split",
            "code": "7203",
            "name": "テスト",
            "market": "プライム",
            "eligibility": {"status": "eligible", "reasons": []},
            "detail": {"ratio": "2"},
            "schedule": build_split_schedule(date(2026, 9, 25)),
        }
        self.assertEqual(due_notifications({"events": [event]}, datetime(2026, 9, 25, 18, 59, tzinfo=JST)), [])
        self.assertIn(
            "分割の空売り検討",
            due_notifications({"events": [event]}, datetime(2026, 9, 25, 19, 0, tzinfo=JST))[0].text,
        )
        noon = due_notifications({"events": [event]}, datetime(2026, 9, 28, 12, 0, tzinfo=JST))
        self.assertTrue(any("大引で空売り" in item.text for item in noon))
        after_close = due_notifications({"events": [event]}, datetime(2026, 9, 28, 15, 30, tzinfo=JST))
        self.assertTrue(all("大引で空売り" not in item.text for item in after_close))


if __name__ == "__main__":
    unittest.main()
