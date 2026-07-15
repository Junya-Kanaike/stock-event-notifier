from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from src.collectors import utils


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_cache_dir = utils.CACHE_DIR
        utils.CACHE_DIR = Path(self.temp_dir.name)

    def tearDown(self):
        os.environ.pop("CACHE_READ_ONLY", None)
        utils.CACHE_DIR = self.original_cache_dir
        self.temp_dir.cleanup()

    def test_cache_uses_embedded_timestamp_instead_of_mtime(self):
        utils.save_json_cache("sample.json", {"7203": "貸借"})
        path = utils.cache_path("sample.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["cache_version"], 1)
        self.assertEqual(utils.load_json_cache("sample.json", max_age=timedelta(days=1)), {"7203": "貸借"})

        payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(utils.load_json_cache("sample.json", max_age=timedelta(days=1)))
        self.assertEqual(utils.load_json_cache("sample.json"), {"7203": "貸借"})

    def test_legacy_cache_is_stale_when_ttl_is_requested(self):
        path = utils.cache_path("legacy.json")
        path.write_text(json.dumps({"7203": "貸借"}), encoding="utf-8")
        self.assertIsNone(utils.load_json_cache("legacy.json", max_age=timedelta(days=1)))
        self.assertEqual(utils.load_json_cache("legacy.json"), {"7203": "貸借"})

    def test_normalize_code_supports_letter_codes_and_safe_tdnet_suffix(self):
        self.assertEqual(utils.normalize_code("598A"), "598A")
        self.assertEqual(utils.normalize_code("72030"), "7203")
        self.assertIsNone(utils.normalize_code("92015"))
        self.assertEqual(utils.normalize_code("コード 7203.0"), "7203")

    def test_read_only_cache_does_not_write(self):
        os.environ["CACHE_READ_ONLY"] = "1"
        utils.save_json_cache("dry-run.json", {"value": 1})
        self.assertFalse(utils.cache_path("dry-run.json").exists())


if __name__ == "__main__":
    unittest.main()
