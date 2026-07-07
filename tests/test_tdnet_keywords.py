import unittest

from src.collectors.tdnet import classify_title, is_po_title


class TdnetKeywordTest(unittest.TestCase):
    def test_po_titles(self):
        titles = [
            "公募による新株式発行及び株式売出しに関するお知らせ",
            "新株式発行及び株式の売出しに関するお知らせ",
            "海外市場における新株式発行及び売出しに関するお知らせ",
        ]
        for title in titles:
            self.assertIn("po", classify_title(title), title)

    def test_po_exclusions_and_pricing(self):
        self.assertFalse(is_po_title("立会外分売に関するお知らせ"))
        self.assertFalse(is_po_title("株主割当による新株式発行に関するお知らせ"))
        self.assertFalse(is_po_title("行使価額修正条項付新株予約権の発行に関するお知らせ"))
        self.assertNotIn("po", classify_title("発行価格及び売出価格等の決定に関するお知らせ"))
        self.assertIn("po_pricing", classify_title("発行価格及び売出価格等の決定に関するお知らせ"))

    def test_cb_titles(self):
        titles = [
            "2029年満期ユーロ円建転換社債型新株予約権付社債発行に関するお知らせ",
            "第三者割当による転換社債型新株予約権付社債の発行に関するお知らせ",
            "転換社債型新株予約権付社債（CB）の発行条件等の決定に関するお知らせ",
        ]
        for title in titles:
            self.assertIn("cb", classify_title(title), title)

    def test_split_titles(self):
        titles = [
            "株式分割及び定款の一部変更に関するお知らせ",
            "株式分割に関する基準日設定公告",
            "株式分割、株式分割に伴う定款の一部変更に関するお知らせ",
        ]
        for title in titles:
            self.assertIn("split", classify_title(title), title)

    def test_bunbai_titles(self):
        titles = [
            "株式の立会外分売に関するお知らせ",
            "立会外分売実施に関するお知らせ",
            "立会外分売の分売条件決定に関するお知らせ",
        ]
        for title in titles:
            self.assertIn("bunbai", classify_title(title), title)


if __name__ == "__main__":
    unittest.main()
