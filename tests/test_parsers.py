from datetime import date
import unittest

from src.core.dateparse import find_dates, parse_date_token
from src.parsers.po_pdf import parse_po_details
from src.parsers.split_pdf import parse_split_details


class ParserTest(unittest.TestCase):
    def test_invalid_date_tokens_are_ignored(self):
        self.assertIsNone(parse_date_token("19/30", default_year=2026))
        self.assertEqual(find_dates("19/30 価格決定日 7/15", default_year=2026), [date(2026, 7, 15)])

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


if __name__ == "__main__":
    unittest.main()
