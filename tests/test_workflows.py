from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class WorkflowConfigTest(unittest.TestCase):
    def test_poll_schedule_uses_off_peak_ten_minute_offsets(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "poll_tdnet.yml").read_text(encoding="utf-8")

        self.assertIn('cron: "3,13,23,33,43,53 23 * * 0-4"', workflow)
        self.assertIn('cron: "3,13,23,33,43,53 0-10 * * 1-5"', workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("Run unit tests", workflow)

    def test_daily_schedule_runs_early_and_keeps_manual_fallback(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "daily_morning.yml").read_text(encoding="utf-8")

        self.assertIn('cron: "17 22 * * 0-4"', workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("Run unit tests", workflow)


if __name__ == "__main__":
    unittest.main()
