from datetime import datetime, timedelta
from pathlib import Path
import plistlib
import unittest
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
        runs["timed_notifications"] = []
        runs["poll_tdnet"] = []
        self.assertEqual(plan_dispatch(NOW, runs, health, {})["dispatch"], "timed_notifications")

    def test_active_shared_concurrency_job_prevents_dispatch(self):
        runs, health = self.fresh()
        runs["timed_notifications"] = []
        runs["poll_tdnet"].append({"id": 22, "status": "queued", "created_at": NOW.isoformat()})
        self.assertIsNone(plan_dispatch(NOW, runs, health, {})["dispatch"])

    def test_long_queued_job_alerts_without_cancelling_it(self):
        runs, health = self.fresh()
        runs["poll_tdnet"] = [{"id": 22, "status": "queued", "created_at": (NOW - timedelta(minutes=25)).isoformat()}]
        result = plan_dispatch(NOW, runs, health, {})
        self.assertIsNone(result["dispatch"])
        self.assertTrue(any("20分以上" in x for x in result["warnings"]))

    def test_recent_dispatch_waits_for_api_visibility(self):
        runs, health = self.fresh()
        runs["timed_notifications"] = []
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


if __name__ == "__main__":
    unittest.main()
