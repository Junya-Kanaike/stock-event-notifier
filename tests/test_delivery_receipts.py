from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests

from scripts.delivery_receipts import ARTIFACT_NAME, decode_receipts, restore
from src.core.bizday import JST
from src.core.delivery import apply_receipts, notification_id, resolve_delivery_hold
from src.core.scheduler import due_notifications
from src.notifiers.slack import SlackDeliveryUncertain, SlackNotifier, post_payload
from src.run_daily import send_due_notifications


NOW = datetime(2026, 11, 4, 8, tzinfo=JST)


class DeliveryReceiptsTest(unittest.TestCase):
    def event(self):
        return {"id": "ipo-test", "type": "ipo", "code": "TEST", "detail": {},
                "eligibility": {"status": "eligible"},
                "schedule": [{"date": "2026-11-04", "label": "listing_day", "sent": False}]}

    def receipt(self, event, outcome="accepted"):
        item = event["schedule"][0]
        return {"schema_version": 1, "notification_id": notification_id(event, item), "event_id": event["id"],
                "scheduled_for": item["date"], "label": item["label"], "outcome": outcome,
                "attempted_at": NOW.isoformat(), "run_id": "123", "reference_only": False}

    def archive(self, records, filename="receipts.jsonl"):
        data = BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr(filename, "\n".join(json.dumps(record) for record in records))
        return data.getvalue()

    def test_successful_send_receipt_survives_state_save_failure(self):
        old = {"events": [self.event()]}
        state = deepcopy(old)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.jsonl"
            with patch.dict(os.environ, {"NOTIFIER_RECEIPTS_PATH": str(path), "GITHUB_RUN_ID": "123"}), patch(
                "src.run_daily.now_jst", return_value=NOW
            ), patch("src.run_daily.save_state", side_effect=OSError("disk failure")), self.assertRaises(OSError):
                send_due_notifications(state, Mock(), NOW, dry_run=False)
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([record["outcome"] for record in records], ["started", "accepted"])
        self.assertEqual(apply_receipts(old, records, "123"), 1)
        self.assertTrue(old["events"][0]["schedule"][0]["sent"])
        self.assertEqual(due_notifications(old, NOW), [])

    def test_cannot_start_send_if_journal_cannot_be_written(self):
        notifier = Mock()
        with patch("src.run_daily.now_jst", return_value=NOW), patch("src.run_daily.write_receipt", side_effect=OSError("full")), self.assertRaises(OSError):
            send_due_notifications({"events": [self.event()]}, notifier, NOW, dry_run=False)
        notifier.send.assert_not_called()

    def test_started_without_result_is_held_and_never_claimed_sent(self):
        event = self.event()
        state = {"events": [event]}
        apply_receipts(state, [self.receipt(event, "started")], "123")
        self.assertFalse(event["schedule"][0]["sent"])
        self.assertEqual(due_notifications(state, NOW), [])
        resolve_delivery_hold(state, notification_id(event, event["schedule"][0]), "not-sent", "Slack channel checked", NOW + timedelta(minutes=1))
        self.assertEqual(len(due_notifications(state, NOW)), 1)
        # Re-reading old receipts cannot undo the explicit review.
        apply_receipts(state, [self.receipt(event, "started")], "123")
        self.assertNotIn("delivery_hold", event["schedule"][0])

    def test_confirmed_failure_remains_retryable(self):
        event = self.event()
        state = {"events": [event]}
        apply_receipts(state, [self.receipt(event, "started"), self.receipt(event, "failed")], "123")
        self.assertEqual(len(due_notifications(state, NOW)), 1)

    def test_accepted_result_cannot_be_overwritten_by_a_later_failed_attempt(self):
        event = self.event()
        state = {"events": [event]}
        apply_receipts(state, [self.receipt(event), self.receipt(event, "failed")], "123")
        self.assertTrue(event["schedule"][0]["sent"])
        self.assertEqual(state["notification_stats"]["2026-11-04"]["success"], 1)
        apply_receipts(state, [self.receipt(event)], "123")
        self.assertEqual(state["notification_stats"]["2026-11-04"]["success"], 1)

    def test_lost_slack_response_stops_automatic_retries(self):
        event = self.event()
        state = {"events": [event]}
        notifier = SlackNotifier(dry_run=False)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SLACK_WEBHOOK_IPO": "https://example.test/mock",
            "NOTIFIER_RECEIPTS_PATH": str(Path(directory) / "receipts.jsonl")}), patch("src.run_daily.now_jst", return_value=NOW), patch(
            "requests.post", side_effect=requests.ReadTimeout("secret response")
        ) as post, patch("src.run_daily.save_state"), patch.object(notifier, "system"):
            self.assertEqual(send_due_notifications(state, notifier, NOW, dry_run=False), 0)
        post.assert_called_once()
        self.assertFalse(event["schedule"][0]["sent"])
        self.assertEqual(state["notification_log"][-1]["outcome"], "unknown")
        self.assertEqual(due_notifications(state, NOW), [])

    def test_only_safe_connect_timeout_is_retried(self):
        response = Mock()
        with patch("requests.post", side_effect=[requests.ConnectTimeout(), response]) as post, patch("src.notifiers.slack.time.sleep"):
            post_payload("https://example.test/mock", {})
        self.assertEqual(post.call_count, 2)
        with patch("requests.post", side_effect=requests.ConnectionError("private-url")) as post, self.assertRaises(SlackDeliveryUncertain) as error:
            post_payload("https://example.test/mock", {})
        post.assert_called_once()
        self.assertNotIn("private-url", str(error.exception))

    def test_rate_limit_respects_retry_after_and_refreshes_cutoff_text(self):
        rejected = requests.Response()
        rejected.status_code = 429
        rejected.headers["Retry-After"] = "7"
        factory = Mock(side_effect=[{"text": "before"}, {"text": "after"}])
        with patch("requests.post", side_effect=[rejected, Mock()]) as post, patch("src.notifiers.slack.time.sleep") as sleep:
            post_payload("https://example.test/mock", {}, payload_factory=factory)
        sleep.assert_called_once_with(7)
        self.assertEqual(post.call_args_list[-1].kwargs["json"]["text"], "after")

    def test_receipt_cannot_mark_a_changed_date_sent(self):
        event = self.event()
        record = self.receipt(event)
        event["schedule"][0]["date"] = "2026-11-05"
        self.assertEqual(apply_receipts({"events": [event]}, [record], "123"), 0)
        self.assertFalse(event["schedule"][0]["sent"])
        record["notification_id"] = "tampered"
        with self.assertRaises(ValueError):
            apply_receipts({"events": [event]}, [record], "123")

    def test_restore_validates_origin_and_skips_applied_artifacts(self):
        event = self.event()
        state = {"events": [event]}
        calls = []
        def get(repo, suffix, **kwargs):
            calls.append(suffix)
            if suffix.startswith("actions/artifacts?"):
                return {"artifacts": [{"id": 42, "name": ARTIFACT_NAME, "workflow_run": {"id": 123}}]}
            if suffix == "actions/runs/123":
                return {"name": "timed_notifications", "head_branch": "main", "event": "workflow_dispatch", "head_repository": {"full_name": repo}}
            return self.archive([self.receipt(event)])
        self.assertEqual(restore(state, "owner/repo", get=get), 1)
        self.assertEqual(state["delivery_receipt_artifacts"], [42])
        calls.clear()
        self.assertEqual(restore(state, "owner/repo", get=get), 0)
        self.assertEqual(len(calls), 1)

    def test_untrusted_pr_cannot_suppress_main_notifications(self):
        event = self.event()
        state = {"events": [event]}
        get = Mock(side_effect=[{"artifacts": [{"id": 42, "name": ARTIFACT_NAME, "workflow_run": {"id": 123}}]},
                               {"name": "timed_notifications", "head_branch": "main", "event": "pull_request", "head_repository": {"full_name": "owner/repo"}}])
        self.assertEqual(restore(state, "owner/repo", get=get), 0)
        self.assertEqual(get.call_count, 2)
        self.assertFalse(event["schedule"][0]["sent"])

    def test_rerun_artifact_keeps_its_attempt_identity(self):
        event = self.event()
        get = Mock(side_effect=[{"artifacts": [{"id": 42, "name": ARTIFACT_NAME + "-123-2", "workflow_run": {"id": 123}}]},
                               {"name": "timed_notifications", "head_branch": "main", "event": "workflow_dispatch", "head_repository": {"full_name": "owner/repo"}},
                               self.archive([self.receipt(event)])])
        self.assertEqual(restore({"events": [event]}, "owner/repo", get=get), 1)

    def test_zip_paths_are_never_extracted(self):
        with self.assertRaises(ValueError):
            decode_receipts(self.archive([], "../../state/events.json"))


if __name__ == "__main__":
    unittest.main()
