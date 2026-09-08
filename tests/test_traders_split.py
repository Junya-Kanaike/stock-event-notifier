from datetime import date
import unittest

from src.collectors.traders_split import parse_traders_split_html
from src.core.split import apply_traders_split
from src.notifiers.slack import SlackNotifier
from src.run_poll import reconcile_split_traders


class TradersSplitTest(unittest.TestCase):
    def test_parses_year_rights_date_ratio_and_rollover(self):
        html = """
        <div class="zone_title_large">2026年</div>
        <table><tr><th>権利取最終日</th><th>銘柄名<br>(コード/市場)</th><th>比率</th><th>効力発生日</th></tr>
        <tr><td>09/28</td><td>ほくほく<br>(8377/東P)</td><td>1→10</td><td>10/01</td></tr>
        <tr><td>12/28</td><td>ホシザキ<br>(6465/東P)</td><td>1→2</td><td>01/01</td></tr></table>
        """
        records = parse_traders_split_html(html)
        self.assertEqual(records["8377"][0]["rights_final_date"], "2026-09-28")
        self.assertEqual(records["8377"][0]["ratio"], "10")
        self.assertEqual(records["6465"][0]["effective_date"], "2027-01-01")

    def test_applies_confirmed_fallback_and_fills_missing_ratio(self):
        detail = {"rights_final_date": None, "ratio": None, "effective_date": "2026-10-01"}
        changed = apply_traders_split(
            detail,
            {
                "rights_final_date": "2026-09-28",
                "ratio": "10",
                "effective_date": "2026-10-01",
                "source_url": "https://www.traders.co.jp/stock_data/split",
            },
        )
        self.assertTrue(changed)
        self.assertEqual(detail["rights_final_date"], "2026-09-28")
        self.assertEqual(detail["ratio"], "10")
        self.assertTrue(detail["rights_final_confirmed"])
        self.assertEqual(detail["rights_final_source"], "traders_web")

    def test_late_reference_resolution_does_not_replay_past_split_actions(self):
        state = {
            "events": [
                {
                    "id": "split-6834-2026-07-17",
                    "type": "split",
                    "code": "6834",
                    "name": "精工技研",
                    "market": "スタンダード",
                    "margin": "貸借",
                    "announced_at": "2026-07-17T15:30:00+09:00",
                    "detail": {"rights_final_date": None, "effective_date": "2026-09-01"},
                    "schedule": [],
                }
            ]
        }
        records = {
            "6834": [
                {
                    "rights_final_date": "2026-08-27",
                    "effective_date": "2026-09-01",
                    "ratio": "5",
                    "source_url": "https://www.traders.co.jp/stock_data/split",
                }
            ]
        }
        self.assertTrue(
            reconcile_split_traders(
                state, records, SlackNotifier(dry_run=True), as_of=date(2026, 9, 8)
            )
        )
        self.assertTrue(all(item["sent"] for item in state["events"][0]["schedule"]))
        self.assertTrue(
            all(
                item["suppressed_reason"] == "reference_resolved_after_scheduled_date"
                for item in state["events"][0]["schedule"]
            )
        )


if __name__ == "__main__":
    unittest.main()
