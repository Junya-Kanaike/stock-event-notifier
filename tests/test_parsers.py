from datetime import date
from pathlib import Path
import unittest

from src.core.dateparse import find_dates, parse_date_token
from src.parsers.bunbai_pdf import parse_bunbai_details
from src.parsers.po_pdf import parse_po_details
from src.parsers.split_pdf import parse_split_details


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tdnet"


class ParserTest(unittest.TestCase):
    def test_invalid_date_tokens_are_ignored(self):
        self.assertIsNone(parse_date_token("19/30", default_year=2026))
        self.assertEqual(find_dates("19/30 価格決定日 7/15", default_year=2026), [date(2026, 7, 15)])
        self.assertEqual(find_dates("受渡期日 2026 年 7 月 29 日"), [date(2026, 7, 29)])

    def test_po_parser_extracts_required_fields(self):
        text = """
        発行価額の総額 10,000百万円
        売出価額の総額 5,000百万円
        発行済株式総数に対する割合 8.3%
        発行価格等決定日 2026年7月8日から2026年7月10日までの間のいずれかの日
        """
        detail = parse_po_details("公募による新株式発行及び株式売出しに関するお知らせ", text, date(2026, 7, 7))
        self.assertEqual(detail["po_kind"], "both")
        self.assertEqual(detail["size_oku"], 150.0)
        self.assertEqual(detail["dilution_pct"], 8.3)
        self.assertEqual(detail["pricing_date"], "2026-07-08")
        self.assertEqual(detail["pricing_date_end"], "2026-07-10")
        self.assertEqual(detail["settlement_date"], "2026-07-16")
        self.assertTrue(detail["settlement_estimated"])

    def test_split_parser_extracts_ratio_and_effective_date(self):
        text = "普通株式1株を2株に分割いたします。効力発生日 2026年8月1日"
        detail = parse_split_details(text, date(2026, 7, 7))
        self.assertEqual(detail["ratio"], "2")
        self.assertEqual(detail["effective_date"], "2026-08-01")

    def test_po_parser_does_not_use_unrelated_dates_as_pricing_date(self):
        detail = parse_po_details("公募による新株式発行", "取締役会決議日 2026年7月13日", date(2026, 7, 13))
        self.assertIsNone(detail["pricing_date"])

    def test_bunbai_parser_extracts_execution_date_near_keyword(self):
        detail = parse_bunbai_details("分売実施予定日 2026年7月21日", date(2026, 7, 15))
        self.assertEqual(detail["execution_date"], "2026-07-21")
        self.assertFalse(detail["execution_date_confirmed"])

    def test_real_tdnet_reit_price_decision_extracts_total_size_and_settlement(self):
        text = (FIXTURE_DIR / "3282_price_decision_20260716.txt").read_text(encoding="utf-8")
        title = "新投資口発行及び投資口売出しに係る価格等の決定に関するお知らせ"

        detail = parse_po_details(title, text, date(2026, 7, 16))

        self.assertEqual(detail["po_kind"], "both")
        self.assertEqual(detail["size_oku"], 110.42)
        self.assertEqual(detail["size_status"], "confirmed")
        self.assertEqual(detail["settlement_date"], "2026-08-04")
        self.assertEqual(detail["settlement_date_status"], "confirmed")
        self.assertFalse(detail["settlement_estimated"])

    def test_real_tdnet_preliminary_terms_extracts_date_range(self):
        text = (FIXTURE_DIR / "543A_preliminary_terms_20260715.txt").read_text(encoding="utf-8")
        title = "株式の売出しに係る仮条件等の決定に関するお知らせ"

        detail = parse_po_details(title, text, date(2026, 7, 15))

        self.assertEqual(detail["po_kind"], "secondary")
        self.assertIsNone(detail["size_oku"])
        self.assertEqual(detail["size_oku_min"], 2265.09)
        self.assertEqual(detail["size_oku_max"], 2718.1)
        self.assertEqual(detail["size_status"], "estimated")
        self.assertEqual(detail["share_count"], 787_855_700)
        self.assertEqual(detail["oa_share_count"], 118_178_300)
        self.assertEqual(detail["pricing_date"], "2026-07-22")
        self.assertEqual(detail["pricing_date_end"], "2026-07-27")
        self.assertEqual(detail["pricing_date_status"], "provisional")
        self.assertEqual(detail["settlement_date"], "2026-07-29")
        self.assertEqual(detail["settlement_date_end"], "2026-08-03")
        self.assertEqual(detail["settlement_date_status"], "provisional")
        self.assertFalse(detail["settlement_estimated"])

    def test_real_tdnet_correction_original_extracts_explicit_sale_details(self):
        text = (FIXTURE_DIR / "4071_original_20260714.txt").read_text(encoding="utf-8")
        title = "ラクスとの資本業務提携に関する覚書の締結、当社株式の売出しに関するお知らせ"

        detail = parse_po_details(title, text, date(2026, 7, 14))

        self.assertEqual(detail["po_kind"], "secondary")
        self.assertEqual(detail["size_oku"], 231.35)
        self.assertEqual(detail["size_status"], "confirmed")
        self.assertEqual(detail["dilution_pct"], 16.29)
        self.assertEqual(detail["pricing_date"], "2026-07-14")
        self.assertTrue(detail["pricing_date_confirmed"])
        self.assertEqual(detail["pricing_date_status"], "confirmed")
        self.assertEqual(detail["settlement_date"], "2026-08-21")
        self.assertEqual(detail["settlement_date_status"], "provisional")


if __name__ == "__main__":
    unittest.main()
