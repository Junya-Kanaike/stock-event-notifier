from datetime import date
import unittest

from src.core.po import apply_reference_close, merge_po_details, refresh_calculated_po_size
from src.parsers.po_pdf import parse_po_details


class PoSizingTest(unittest.TestCase):
    def test_pricing_title_allows_confirmed_price_without_repeated_decision_word(self):
        detail = parse_po_details(
            "発行価格及び売出価格等の決定に関するお知らせ",
            "募集株式数 1,000,000株 発行価格 1株につき1,250円",
            date(2026, 9, 8),
        )
        self.assertEqual(detail["issue_price_yen"], 1250.0)

    def test_share_breakdown_and_announcement_close_formula(self):
        text = """
        公募による新株式発行 募集株式数 1,000,000株
        株式の売出し 売出株式数 2,000,000株
        オーバーアロットメントによる売出し 売出株式数 300,000株
        """
        detail = parse_po_details("公募による新株式発行及び株式の売出し", text, date(2026, 9, 7))
        apply_reference_close(
            detail,
            {"close_yen": 2_500, "date": "2026-09-07", "source": "Yahoo Finance chart (raw close)"},
            "announcement_close",
        )
        self.assertEqual(detail["public_offering_shares"], 1_000_000)
        self.assertEqual(detail["secondary_sale_shares"], 2_000_000)
        self.assertEqual(detail["oa_shares"], 300_000)
        self.assertEqual(detail["effective_size_yen"], 8_250_000_000)
        self.assertTrue(detail["po_threshold_ever_met"])

    def test_confirmed_price_replaces_estimate_but_keeps_threshold_latch(self):
        detail = {
            "po_kind": "secondary",
            "secondary_sale_shares": 10_000_000,
            "oa_shares": 0,
            "total_offered_shares": 10_000_000,
            "reference_close_yen": 900,
            "reference_close_date": "2026-09-07",
            "reference_close_kind": "announcement_close",
            "po_threshold_ever_met": False,
        }
        refresh_calculated_po_size(detail)
        self.assertTrue(detail["po_threshold_ever_met"])
        detail.update({"sale_price_yen": 700, "offer_price_yen": 700})
        refresh_calculated_po_size(detail)
        self.assertEqual(detail["effective_size_yen"], 7_000_000_000)
        self.assertTrue(detail["po_threshold_ever_met"])

    def test_valid_positive_size_is_not_overwritten_by_zero(self):
        merged = merge_po_details(
            {"size_oku": 150.0, "total_offered_shares": 3_000_000},
            {"size_oku": 0.0, "total_offered_shares": 0, "source_stage": "correction"},
        )
        self.assertEqual(merged["size_oku"], 150.0)
        self.assertEqual(merged["total_offered_shares"], 3_000_000)


if __name__ == "__main__":
    unittest.main()
