from datetime import date
import unittest

from src.core.bizday import add_business_days, is_business_day, next_business_day, prev_business_day


class BusinessDayTest(unittest.TestCase):
    def test_weekend_holiday_and_year_end(self):
        self.assertFalse(is_business_day(date(2026, 1, 1)))
        self.assertFalse(is_business_day(date(2026, 1, 3)))
        self.assertFalse(is_business_day(date(2026, 1, 12)))
        self.assertFalse(is_business_day(date(2026, 7, 18)))
        self.assertTrue(is_business_day(date(2026, 7, 21)))

    def test_add_business_days_across_year_end(self):
        self.assertEqual(add_business_days(date(2025, 12, 30), 1), date(2026, 1, 5))
        self.assertEqual(prev_business_day(date(2026, 1, 5)), date(2025, 12, 30))

    def test_add_business_days_across_japanese_holiday(self):
        self.assertEqual(next_business_day(date(2026, 7, 17)), date(2026, 7, 21))
        self.assertEqual(add_business_days(date(2026, 7, 17), 2), date(2026, 7, 22))


if __name__ == "__main__":
    unittest.main()
