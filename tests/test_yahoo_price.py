from datetime import date, datetime, timezone
import json
import unittest
from unittest.mock import patch

from src.collectors.yahoo_price import fetch_close_on_or_before, reference_close_date
from src.core.bizday import JST


class YahooPriceTest(unittest.TestCase):
    def test_pre_close_uses_previous_business_day(self):
        target, kind = reference_close_date(
            datetime(2026, 9, 7, 14, 0, tzinfo=JST),
            as_of=datetime(2026, 9, 7, 15, 0, tzinfo=JST),
        )
        self.assertEqual(target, date(2026, 9, 4))
        self.assertEqual(kind, "previous_close_pre_close")

    def test_after_close_uses_announcement_day(self):
        target, kind = reference_close_date(
            datetime(2026, 9, 7, 14, 0, tzinfo=JST),
            as_of=datetime(2026, 9, 7, 15, 30, tzinfo=JST),
        )
        self.assertEqual(target, date(2026, 9, 7))
        self.assertEqual(kind, "announcement_close")

    def test_fetch_uses_raw_quote_close_not_adjusted_close(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [int(datetime(2026, 9, 7, tzinfo=timezone.utc).timestamp())],
                        "indicators": {
                            "quote": [{"close": [1234.5]}],
                            "adjclose": [{"adjclose": [999.0]}],
                        },
                    }
                ],
                "error": None,
            }
        }
        with patch("src.collectors.yahoo_price.load_json_cache", return_value={}), patch(
            "src.collectors.yahoo_price.save_json_cache"
        ), patch("src.collectors.yahoo_price.request_get", return_value=json.dumps(payload).encode()):
            result = fetch_close_on_or_before("7203", date(2026, 9, 7))
        self.assertEqual(result["close_yen"], 1234.5)
        self.assertIn("raw close", result["source"])

    def test_fetch_does_not_silently_substitute_previous_day(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [int(datetime(2026, 9, 4, tzinfo=timezone.utc).timestamp())],
                        "indicators": {"quote": [{"close": [1234.5]}]},
                    }
                ],
                "error": None,
            }
        }
        with patch("src.collectors.yahoo_price.load_json_cache", return_value={}), patch(
            "src.collectors.yahoo_price.save_json_cache"
        ), patch("src.collectors.yahoo_price.request_get", return_value=json.dumps(payload).encode()):
            with self.assertRaisesRegex(RuntimeError, "on 2026-09-07"):
                fetch_close_on_or_before("7203", date(2026, 9, 7))


if __name__ == "__main__":
    unittest.main()
