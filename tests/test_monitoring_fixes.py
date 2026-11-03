from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import mac_scheduler as mac
from scripts import cloud_dispatcher as cloud
from src.core.bizday import JST
from src.core.historical import hold_historical_po, is_historical_po
from src.core.reconcile import reconcile_event_state
from src.core.scheduler import due_notifications
from src.notifiers.slack import SlackNotifier
from src.run_daily import send_daily_system_summary
from src.run_poll import recover_po_calculations, refresh_po_reference_prices, notify_po_state


NOW = datetime(2026, 11, 4, 10, tzinfo=JST)


class StandaloneMonitorTest(unittest.TestCase):
    def test_deployed_files_monitor_freshness_and_late_delivery_without_checkout(self):
        state = {"source_health": {"jpx_margin": {"last_success_at": "2026-10-01T06:00:00+09:00"}},
                 "events": [{"type": "ipo", "code": "TEST", "eligibility": {"status": "eligible"},
                             "schedule": [{"date": "2026-11-04", "label": "listing_day", "not_before_jst": "06:00"}]}]}
        with tempfile.TemporaryDirectory() as directory:
            support = Path(directory)
            mac.deploy_helper(support)
            code = ("import sys,runpy,json; from datetime import datetime; "
                    "sys.path.insert(0,sys.argv[1]); m=runpy.run_path(sys.argv[1]+'/mac_scheduler.py'); "
                    "print(json.dumps(m['health_warnings'](json.loads(sys.argv[2]),datetime.fromisoformat(sys.argv[3]))))")
            env = dict(os.environ, PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-c", code, directory, json.dumps(state), NOW.isoformat()],
                                    cwd=directory, env=env, check=True, capture_output=True, text=True)
            warnings = json.loads(result.stdout)
            self.assertEqual(len(warnings), 2)
            self.assertIn("34日", warnings[0])
            self.assertIn("未送信", warnings[1])
            self.assertEqual(len(json.loads((support / "installed-version.json").read_text())["sha256"]), 2)

    def test_updater_keeps_local_state_and_backs_up_both_files(self):
        with tempfile.TemporaryDirectory() as directory:
            support = Path(directory)
            mac.deploy_helper(support)
            (support / "watchdog-state.json").write_text('{"kept":true}')
            old = (support / "mac_scheduler.py").read_bytes()
            mac.deploy_helper(support, update=True)
            self.assertEqual((support / "watchdog-state.json").read_text(), '{"kept":true}')
            self.assertEqual(next(support.glob("mac_scheduler.py.*.bak")).read_bytes(), old)
            self.assertEqual(len(list(support.glob("*.bak"))), 2)

    def test_missing_shared_module_fails_visibly_instead_of_silencing_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "mac_scheduler.py"
            target.write_bytes(Path(mac.__file__).read_bytes())
            result = subprocess.run([sys.executable, str(target), "--help"], cwd=directory,
                                    env=dict(os.environ, PYTHONPATH=""), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("monitor_health", result.stderr)

    def test_three_consecutive_api_failures_then_recovery_are_recorded(self):
        local = {}
        error = mac.GitHubReadError("state", "CalledProcessError", 503)
        with patch.object(mac, "inspect_remote", side_effect=error):
            for minute in range(3):
                plan = mac.inspect_safely("gh", "repo", NOW + timedelta(minutes=minute), local)
                self.assertIsNone(plan["dispatch"])
                self.assertEqual(plan["github_check"]["consecutive_failures"], minute + 1)
                self.assertEqual(bool(plan["warnings"]), minute == 2)
        self.assertIn("HTTP 503", plan["warnings"][0])
        with patch.object(mac, "inspect_remote", return_value={"dispatch": None, "warnings": []}):
            recovered = mac.inspect_safely("gh", "repo", NOW + timedelta(minutes=3), local)
        self.assertEqual(recovered["warnings"], [])
        self.assertEqual(local["github_health"]["consecutive_failures"], 0)
        self.assertEqual(local["github_health"]["previous_failures"], 3)
        self.assertIn("recovered_at", local["github_health"])

    def test_api_diagnostics_discard_credentials_and_unrecognized_endpoints(self):
        error = subprocess.CalledProcessError(1, "gh", stderr="secret-token private-url (HTTP 403)")
        with patch.object(mac.subprocess, "run", side_effect=error), self.assertRaises(mac.GitHubReadError) as caught:
            mac.gh_json("gh", "repos/owner/private/contents/state/events.json?ref=main")
        self.assertEqual(caught.exception.details, {"operation": "state", "kind": "CalledProcessError", "http_status": 403})
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_cloud_transient_failure_does_not_alert_or_dispatch(self):
        local = {}
        notifier = Mock()
        with patch.object(cloud, "inspect_remote", side_effect=mac.GitHubReadError("state", "TimeoutExpired")), patch.object(cloud, "request_workflow") as post:
            for minute in range(2):
                cloud.dispatch_cycle("gh", "repo", NOW + timedelta(minutes=minute), local, live=True, notifier=notifier)
            notifier.system.assert_not_called()
            cloud.dispatch_cycle("gh", "repo", NOW + timedelta(minutes=2), local, live=True, notifier=notifier)
        notifier.system.assert_called_once()
        post.assert_not_called()


class HistoricalPOTest(unittest.TestCase):
    def event(self):
        return {"id": "po-old", "code": "TEST", "type": "po", "market": "プライム", "margin": "貸借",
                "announced_at": "2026-07-15", "schedule": [], "pdf_url": "https://example.test/old.pdf",
                "eligibility": {"status": "pending", "reasons": ["吸収規模未確定"]},
                "detail": {"pricing_date": "2026-07-22", "settlement_date": "2026-08-21",
                           "calculation_recovery_last_error": "expired", "effective_size_yen": None}}

    def test_old_unrecoverable_po_is_retained_without_false_resolution(self):
        state = {"schema_version": 2, "events": [self.event()]}
        self.assertTrue(reconcile_event_state(state, as_of=NOW.date()))
        event = state["events"][0]
        self.assertTrue(is_historical_po(event))
        self.assertEqual(event["eligibility"]["status"], "pending")
        self.assertIsNone(event["detail"]["effective_size_yen"])
        self.assertEqual(event["schedule"], [])
        self.assertFalse(reconcile_event_state(state, as_of=NOW.date()))
        with patch("src.run_poll.fetch_pdf_text") as pdf, patch("src.run_poll.apply_po_reference_price") as price:
            recover_po_calculations(state, Mock(), now=NOW)
            refresh_po_reference_prices(state, Mock(), now=NOW)
        pdf.assert_not_called()
        price.assert_not_called()
        notifier = Mock()
        notify_po_state(notifier, event, "confirmed")
        notifier.send.assert_not_called()

    def test_recent_future_dated_or_untried_po_is_not_held(self):
        for field, value in [("announced_at", "2026-10-01"), ("settlement_date", "2026-12-01"),
                             ("calculation_recovery_last_error", None), ("pricing_date", "invalid")]:
            event = self.event()
            (event if field == "announced_at" else event["detail"])[field] = value
            hold_historical_po(event, NOW.date())
            self.assertFalse(is_historical_po(event), field)

    def test_historical_po_is_separate_from_current_unresolved_summary(self):
        event = self.event()
        hold_historical_po(event, NOW.date())
        notifier = SlackNotifier(dry_run=True)
        send_daily_system_summary({"events": [event]}, notifier, NOW, failures=[])
        text = notifier.sent_messages[0]["payload"]["text"]
        self.assertIn("過去POの履歴保留: 1件 (TEST)", text)
        self.assertIn("判定待ち: 0件", text)
        self.assertIn("イベント別未解決: 0件", text)

    def test_historical_hold_blocks_newly_rebuilt_old_schedule(self):
        event = self.event()
        hold_historical_po(event, NOW.date())
        event["eligibility"]["status"] = "eligible"
        event["schedule"] = [{"date": "2026-11-04", "label": "pricing_day", "sent": False}]
        self.assertEqual(due_notifications({"events": [event]}, NOW), [])
