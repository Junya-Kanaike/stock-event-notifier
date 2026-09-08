import unittest

from src.core.eligibility import ELIGIBLE, EXCLUDED, PENDING, evaluate_event


def event(event_type, market, margin, **detail):
    return {"type": event_type, "market": market, "margin": margin, "detail": detail}


class EligibilityTest(unittest.TestCase):
    def test_ipo_excludes_only_pro_market(self):
        self.assertEqual(
            evaluate_event(event("ipo", "名証メイン", "対象外", listing_date="2026-09-10"))["status"],
            ELIGIBLE,
        )
        self.assertEqual(
            evaluate_event(event("ipo", "TOKYO PRO Market", "対象外", listing_date="2026-09-10"))["status"],
            EXCLUDED,
        )
        self.assertEqual(evaluate_event(event("ipo", "名証メイン", "対象外"))["status"], PENDING)

    def test_po_requires_margin_and_80_oku_latch(self):
        eligible = event("po", "プライム", "信用", effective_size_yen=8_000_000_000, po_threshold_ever_met=True)
        below = event("po", "プライム", "貸借", effective_size_yen=7_999_999_999, po_threshold_ever_met=False)
        missing = event("po", "プライム", "貸借", effective_size_yen=None, po_threshold_ever_met=False)
        self.assertEqual(evaluate_event(eligible)["status"], ELIGIBLE)
        self.assertEqual(evaluate_event(below)["status"], EXCLUDED)
        self.assertEqual(evaluate_event(missing)["status"], PENDING)

    def test_bunbai_is_tse_only_but_all_three_segments(self):
        self.assertEqual(evaluate_event(event("bunbai", "グロース", "信用", execution_date="2026-09-10"))["status"], ELIGIBLE)
        self.assertEqual(evaluate_event(event("bunbai", "名証メイン", "貸借", execution_date="2026-09-10"))["status"], EXCLUDED)

    def test_split_is_prime_or_standard_and_margin_loan_only(self):
        self.assertEqual(evaluate_event(event("split", "プライム", "貸借", rights_final_date="2026-09-25"))["status"], ELIGIBLE)
        self.assertEqual(evaluate_event(event("split", "グロース", "貸借", rights_final_date="2026-09-25"))["status"], EXCLUDED)
        self.assertEqual(evaluate_event(event("split", "スタンダード", "信用", rights_final_date="2026-09-25"))["status"], EXCLUDED)

    def test_cb_requires_margin_loan(self):
        self.assertEqual(evaluate_event(event("cb", "スタンダード", "貸借"))["status"], ELIGIBLE)
        self.assertEqual(evaluate_event(event("cb", "スタンダード", "信用"))["status"], EXCLUDED)


if __name__ == "__main__":
    unittest.main()
