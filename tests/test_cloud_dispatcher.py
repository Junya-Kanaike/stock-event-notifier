from datetime import datetime, timedelta
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from scripts import cloud_dispatcher as cloud
from scripts.mac_scheduler import JST


NOW = datetime(2026, 10, 19, 8, 0, tzinfo=JST)


class CloudDispatcherTest(unittest.TestCase):
    def test_default_check_does_not_dispatch_or_alert(self):
        notifier = Mock()
        with patch.object(cloud, "inspect_remote", return_value={"dispatch": "poll_tdnet", "warnings": ["stale"]}), patch.object(cloud, "request_workflow") as post:
            cloud.dispatch_cycle("gh", "repo", NOW, {}, notifier=notifier)
        post.assert_not_called()
        notifier.system.assert_not_called()

    def test_dispatches_only_the_planned_job_on_main(self):
        local = {}
        with patch.object(cloud, "inspect_remote", return_value={"dispatch": "poll_tdnet", "warnings": []}), patch.object(cloud, "request_workflow") as post:
            plan = cloud.dispatch_cycle("gh", "repo", NOW, local, live=True)
        post.assert_called_once_with("gh", "repo", "poll_tdnet.yml")
        self.assertEqual(local["dispatches"]["poll_tdnet"], NOW.isoformat())
        self.assertEqual(plan["request_accepted"], "poll_tdnet")

    def test_uncertain_request_reserves_before_retry(self):
        local = {}
        with patch.object(cloud, "inspect_remote", return_value={"dispatch": "poll_tdnet", "warnings": []}), patch.object(cloud, "request_workflow", side_effect=subprocess.TimeoutExpired("gh", 30)):
            plan = cloud.dispatch_cycle("gh", "repo", NOW, local, live=True)
        self.assertIn("poll_tdnet", local["dispatches"])
        self.assertNotIn("request_accepted", plan)

    def test_active_state_job_is_not_replaced(self):
        with patch.object(cloud, "inspect_remote", return_value={"dispatch": None, "warnings": [], "active_runs": [123]}), patch.object(cloud, "request_workflow") as post:
            cloud.dispatch_cycle("gh", "repo", NOW, {}, live=True)
        post.assert_not_called()

    def test_read_failure_does_not_cause_blind_dispatch(self):
        with patch.object(cloud, "inspect_remote", side_effect=RuntimeError("not-caught")):
            with self.assertRaises(RuntimeError):
                cloud.dispatch_cycle("gh", "repo", NOW, {}, live=True)
        with patch.object(cloud, "inspect_remote", side_effect=ValueError("private-value")), patch.object(cloud, "request_workflow") as post:
            plan = cloud.dispatch_cycle("gh", "repo", NOW, {}, live=True)
        post.assert_not_called()
        self.assertNotIn("private-value", str(plan))

    def test_warning_alert_is_limited_to_once_per_hour(self):
        notifier = Mock()
        local = {}
        with patch.object(cloud, "inspect_remote", side_effect=lambda *a: {"dispatch": None, "warnings": ["stale"]}):
            for minute in [0, 10, 59, 60]:
                cloud.dispatch_cycle("gh", "repo", NOW+timedelta(minutes=minute), local, live=True, notifier=notifier)
        self.assertEqual(notifier.system.call_count, 2)

    def test_handoff_retains_an_already_queued_successor(self):
        runs = {"workflow_runs": [{"id": 1, "status": "in_progress", "head_branch": "main"}, {"id": 2, "status": "queued", "head_branch": "main"}]}
        with patch.object(cloud, "gh_json", return_value=runs), patch.object(cloud, "request_workflow") as post:
            cloud.request_successor("gh", "repo", "1")
        post.assert_not_called()

    def test_handoff_requests_successor_when_no_other_run_exists(self):
        with patch.object(cloud, "gh_json", return_value={"workflow_runs": [{"id": 1, "status": "in_progress", "head_branch": "main"}]}), patch.object(cloud, "request_workflow") as post:
            cloud.request_successor("gh", "repo", "1")
        post.assert_called_once_with("gh", "repo", cloud.WORKFLOW)

    def test_live_is_rejected_locally_or_without_explicit_opt_in(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(cloud, "gh_json") as get:
            with self.assertRaises(RuntimeError):
                cloud.validate_live_environment("gh", cloud.REPOSITORY)
        get.assert_not_called()

    def test_private_repository_cannot_start_continuous_hosted_usage(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main", "NOTIFIER_CONTINUOUS_DISPATCHER": "true"}
        with patch.dict(os.environ, env, clear=True), patch.object(cloud, "gh_json", return_value={"private": True}):
            with self.assertRaises(RuntimeError):
                cloud.validate_live_environment("gh", cloud.REPOSITORY)

    def test_loop_hands_off_before_runtime_limit(self):
        with patch.object(cloud, "dispatch_cycle", return_value={"warnings": []}) as cycle, patch.object(cloud, "request_successor") as handoff:
            result = cloud.run_loop("gh", "repo", seconds=1, now=lambda: NOW, sleep=Mock(), monotonic=lambda: 0)
        self.assertEqual(result, 0)
        cycle.assert_called_once()
        handoff.assert_called_once()

    def test_live_posts_use_main_without_shell_expansion(self):
        with patch.object(cloud.subprocess, "run") as run:
            cloud.request_workflow("gh", "repo", "poll_tdnet.yml")
        self.assertEqual(run.call_args.args[0][-2:], ["--ref", "main"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_long_running_monitor_hands_off_without_waiting_for_cron(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(cloud, "dispatch_cycle", return_value={"warnings": []}) as cycle, patch.object(cloud, "request_successor") as handoff:
            cloud.run_loop("gh", "repo", monotonic=lambda: clock[0], sleep=sleep,
                           now=lambda: NOW+timedelta(seconds=clock[0]))
        self.assertGreaterEqual(cycle.call_count, 160)
        self.assertLess(clock[0], cloud.RUN_SECONDS)
        handoff.assert_called_once()

    def test_handoff_failure_is_bounded_and_not_hidden(self):
        sleep = Mock()
        with patch.object(cloud, "dispatch_cycle", return_value={"warnings": []}), patch.object(
            cloud, "request_successor", side_effect=subprocess.TimeoutExpired("gh", 30)
        ) as handoff:
            with self.assertRaises(RuntimeError):
                cloud.run_loop("gh", "repo", seconds=1, monotonic=lambda: 0, sleep=sleep, now=lambda: NOW)
        self.assertEqual(handoff.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
