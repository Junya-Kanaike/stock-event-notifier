from datetime import datetime, timedelta
import json
from pathlib import Path
import plistlib
import subprocess
import unittest
from unittest.mock import patch

from scripts import mac_scheduler
from scripts.mac_scheduler import JST, WORKFLOWS, freshness_requirement, plan_dispatch, launchd_config

NOW = datetime(2026, 9, 16, 8, 30, tzinfo=JST)


class MacSchedulerTest(unittest.TestCase):
    def fresh(self):
        health = {name: {"last_success_at": NOW.isoformat()} for name in WORKFLOWS}
        runs = {name: [{"id": name, "status": "completed", "conclusion": "success",
                        "created_at": NOW.isoformat()}] for name in WORKFLOWS}
        return runs, health

    def test_no_dispatch_when_all_success_evidence_is_fresh(self):
        runs, health = self.fresh()
        self.assertIsNone(plan_dispatch(NOW, runs, health, {})["dispatch"])

    def test_missing_production_upgrade_is_not_dispatched(self):
        self.assertIsNone(plan_dispatch(NOW, {}, {}, {})["dispatch"])

    def test_missing_timed_run_is_dispatched_before_poll(self):
        runs, health = self.fresh()
        health["timed_notifications"] = {}
        health["poll_tdnet"] = {}
        self.assertEqual(plan_dispatch(NOW, runs, health, {})["dispatch"], "timed_notifications")

    def test_active_shared_concurrency_job_prevents_dispatch(self):
        runs, health = self.fresh()
        health["timed_notifications"] = {}
        runs["poll_tdnet"].append({"id": 22, "status": "queued", "created_at": NOW.isoformat()})
        result = plan_dispatch(NOW, runs, health, {})
        self.assertEqual(result["due"], ["timed_notifications"])
        self.assertIsNone(result["dispatch"])

    def test_run_stuck_in_queue_for_hours_does_not_block_dispatch(self):
        # 2026-10-23: a daily_morning run stayed queued for 39 hours and both
        # helpers stopped dispatching; the morning notice went out at 08:33.
        now = datetime(2026, 10, 23, 6, 5, tzinfo=JST)
        stuck = {"id": 39800624291, "status": "queued", "created_at": "2026-10-22T14:59:42Z"}
        health = {name: {"last_success_at": "2026-10-22T23:58:38+09:00"} for name in WORKFLOWS}
        runs = {name: [] for name in WORKFLOWS}
        runs["daily_morning"] = [stuck]
        result = plan_dispatch(now, runs, health, {})
        self.assertEqual(result["dispatch"], "daily_morning")
        self.assertEqual(result["active_runs"], [])
        self.assertTrue(any("365分止まっている実行を無視" in x and "39800624291" in x for x in result["warnings"]))

    def test_saved_success_is_enough_when_run_list_is_incomplete(self):
        # 2026-10-22 22:57: during a GitHub incident the run list lacked the
        # morning run, and the daily sync was requested again although its
        # 08:35 success was saved.
        now = datetime(2026, 10, 22, 22, 57, tzinfo=JST)
        health = {name: {"last_success_at": "2026-10-22T08:35:37+09:00"} for name in WORKFLOWS}
        result = plan_dispatch(now, {name: [] for name in WORKFLOWS}, health, {})
        self.assertIsNone(result["dispatch"])
        self.assertEqual(result["due"], [])
        self.assertEqual(result["warnings"], [])

    def test_alert_measures_delay_from_expected_time(self):
        yesterday = {"last_success_at": "2026-09-15T08:35:00+09:00"}
        runs = {name: [] for name in WORKFLOWS}

        early = plan_dispatch(NOW.replace(hour=5, minute=50), runs, {"daily_morning": yesterday}, {})
        self.assertEqual(early["dispatch"], "daily_morning")
        self.assertEqual(early["warnings"], [])

        late = plan_dispatch(NOW.replace(hour=6, minute=5), runs, {"daily_morning": yesterday}, {})
        self.assertEqual(late["warnings"], ["daily_morning: 予定時刻から35分経っても処理成功が保存されていません"])

    def test_first_poll_of_the_day_is_not_alerted_for_overnight_age(self):
        now = NOW.replace(hour=8, minute=1)
        health = {name: {"last_success_at": now.isoformat()} for name in WORKFLOWS}
        health["poll_tdnet"] = {"last_success_at": "2026-09-15T19:52:00+09:00"}
        result = plan_dispatch(now, {name: [] for name in WORKFLOWS}, health, {})
        self.assertEqual(result["dispatch"], "poll_tdnet")
        self.assertEqual(result["warnings"], [])

        later = now.replace(hour=10, minute=0)
        health = {name: {"last_success_at": later.isoformat()} for name in WORKFLOWS}
        health["poll_tdnet"] = {"last_success_at": later.replace(hour=9, minute=15).isoformat()}
        stalled = plan_dispatch(later, {name: [] for name in WORKFLOWS}, health, {})
        self.assertEqual(stalled["warnings"], ["poll_tdnet: 予定時刻から35分経っても処理成功が保存されていません"])

    def test_long_queued_job_alerts_without_cancelling_it(self):
        runs, health = self.fresh()
        runs["poll_tdnet"] = [{"id": 22, "status": "queued", "created_at": (NOW - timedelta(minutes=25)).isoformat()}]
        result = plan_dispatch(NOW, runs, health, {})
        self.assertIsNone(result["dispatch"])
        self.assertTrue(any("20分以上" in x for x in result["warnings"]))

    def test_recent_dispatch_waits_for_api_visibility(self):
        runs, health = self.fresh()
        health["timed_notifications"] = {}
        local = {"dispatches": {"timed_notifications": NOW.isoformat()}}
        self.assertIsNone(plan_dispatch(NOW, runs, health, local)["dispatch"])

    def test_successful_job_with_stale_saved_health_is_retried(self):
        runs, health = self.fresh()
        health["timed_notifications"]["last_success_at"] = (NOW - timedelta(hours=1)).isoformat()
        self.assertEqual(plan_dispatch(NOW, runs, health, {})["dispatch"], "timed_notifications")

    def test_weekend_only_daily_sync_is_supported(self):
        saturday = datetime(2026, 9, 19, 8, tzinfo=JST)
        self.assertIsNone(freshness_requirement("poll_tdnet", saturday))
        self.assertIsNone(freshness_requirement("timed_notifications", saturday))
        self.assertIsNotNone(freshness_requirement("daily_morning", saturday))

    def test_no_evening_notice_before_time_gate(self):
        self.assertEqual(freshness_requirement("timed_notifications", NOW.replace(hour=18, minute=59)),
                         NOW.replace(hour=18, minute=29))
        self.assertEqual(freshness_requirement("timed_notifications", NOW.replace(hour=19, minute=0)),
                         NOW.replace(hour=19, minute=0))

    def test_japanese_holiday_has_no_intraday_dispatch(self):
        holiday = datetime(2026, 9, 21, 8, tzinfo=JST)
        self.assertIsNone(freshness_requirement("poll_tdnet", holiday))

    def test_before_gate_success_does_not_satisfy_after_gate_notice(self):
        now = NOW.replace(hour=8, minute=0)
        runs, health = self.fresh()
        for name in WORKFLOWS:
            runs[name][0]["created_at"] = (now - timedelta(minutes=1)).isoformat()
            health[name]["last_success_at"] = (now - timedelta(minutes=1)).isoformat()
        self.assertEqual(plan_dispatch(now, runs, health, {})["dispatch"], "timed_notifications")

    def test_launchd_argument_paths_with_spaces_roundtrip(self):
        config = launchd_config("/path with spaces/python", "/path with spaces/helper.py",
                                "/opt/homebrew/bin/gh", "owner/repo", Path("/tmp/space dir"))
        parsed = plistlib.loads(plistlib.dumps(config))
        self.assertEqual(parsed["ProgramArguments"][0], "/path with spaces/python")
        self.assertEqual(parsed["StartInterval"], 60)
        self.assertNotIn("EnvironmentVariables", parsed)

    def test_state_over_one_megabyte_is_read_with_raw_media_type(self):
        _, health = self.fresh()
        state = {"workflow_health": health, "notification_log": ["x" * 1024] * 1200}
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            body = state if args[-1].startswith("repos/owner/repo/contents/") else {"workflow_runs": []}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(body), stderr="")

        with patch.object(mac_scheduler.subprocess, "run", side_effect=fake_run):
            plan = mac_scheduler.inspect_remote("gh", "owner/repo", NOW, {})

        contents_call = next(args for args in calls if "contents/state/events.json" in args[-1])
        self.assertIn("Accept: application/vnd.github.raw+json", contents_call)
        self.assertGreater(len(json.dumps(state)), 1024 * 1024)
        self.assertIn("dispatch", plan)


if __name__ == "__main__":
    unittest.main()
