from datetime import date, datetime
import unittest

from src.collectors.jpx_ex_rights import find_latest_excel_url, parse_ex_rights_rows
from src.core.split import apply_jpx_ex_right
from src.parsers.split_pdf import parse_split_details


class ExRightsTest(unittest.TestCase):
    def test_finds_latest_jpx_excel(self):
        html = """
        <a href="/files/20260904.xls">old</a>
        <a href="/files/20260907.xls">new</a>
        """
        self.assertEqual(
            find_latest_excel_url(html, "https://www.jpx.co.jp/listing/others/ex-rights/index.html"),
            "https://www.jpx.co.jp/files/20260907.xls",
        )

    def test_parses_split_rows_only(self):
        rows = [
            ["基準日", "（実質上）基準日", "権利落日\n（普通取引）", "銘柄コード", "銘柄略称", "市場", "備考"],
            [46280.0, 46280.0, 46279.0, 28140.0, "佐藤食品", "スタンダード市場", "分割"],
            [46280.0, 46280.0, 46279.0, 99990.0, "配当会社", "プライム市場", "配当"],
        ]
        records = parse_ex_rights_rows(rows)
        self.assertEqual(set(records), {"2814"})
        self.assertEqual(records["2814"]["ex_right_date"], "2026-09-14")
        self.assertEqual(records["2814"]["rights_final_date"], "2026-09-11")
        self.assertEqual(records["2814"]["record_date"], "2026-09-15")

    def test_split_parser_prefers_explicit_rights_final_date(self):
        detail = parse_split_details(
            "権利付最終日 2026年9月25日 基準日 2026年9月30日 効力発生日 2026年10月1日",
            date(2026, 8, 7),
        )
        self.assertEqual(detail["rights_final_date"], "2026-09-25")
        self.assertTrue(detail["rights_final_confirmed"])
        self.assertEqual(detail["rights_final_source"], "pdf_explicit")
        self.assertEqual(detail["ex_right_date"], "2026-09-28")
        self.assertEqual(detail["rights_final_status"], "confirmed")

    def test_split_parser_calculates_from_weekend_record_date(self):
        detail = parse_split_details("基準日 2026年10月31日", date(2026, 9, 7))
        self.assertEqual(detail["record_date"], "2026-10-31")
        self.assertEqual(detail["rights_final_date"], "2026-10-28")
        self.assertFalse(detail["rights_final_confirmed"])
        self.assertEqual(detail["rights_final_status"], "provisional")

    def test_jpx_mismatch_marks_conflict_and_matching_date_confirms(self):
        mismatch = {"rights_final_date": "2026-09-24", "rights_final_confirmed": False}
        record = {
            "rights_final_date": "2026-09-25",
            "ex_right_date": "2026-09-28",
            "source_url": "https://www.jpx.co.jp/example.xls",
        }
        apply_jpx_ex_right(mismatch, record)
        self.assertTrue(mismatch["rights_date_conflict"])
        self.assertEqual(mismatch["rights_final_status"], "conflict")

        matching = {"rights_final_date": "2026-09-25", "rights_final_confirmed": False}
        apply_jpx_ex_right(matching, record)
        self.assertTrue(matching["rights_final_confirmed"])
        self.assertEqual(matching["ex_right_date"], "2026-09-28")

    def test_explicit_rights_final_on_weekend_is_conflict(self):
        detail = parse_split_details("権利付最終日 2026年9月26日", date(2026, 9, 8))
        self.assertTrue(detail["rights_date_conflict"])
        self.assertFalse(detail["rights_final_confirmed"])


if __name__ == "__main__":
    unittest.main()
