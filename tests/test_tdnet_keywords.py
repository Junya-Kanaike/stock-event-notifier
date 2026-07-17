from datetime import date
import unittest
from unittest.mock import patch

from src.collectors import tdnet
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
        self.assertFalse(is_po_title("譲渡制限付株式報酬としての新株式発行に関するお知らせ"))
        self.assertNotIn("po", classify_title("発行価格及び売出価格等の決定に関するお知らせ"))
        self.assertIn("po_pricing", classify_title("発行価格及び売出価格等の決定に関するお知らせ"))

    def test_po_pricing_recognizes_generic_price_decision_but_not_preliminary_terms(self):
        title = "新投資口発行及び投資口売出しに係る価格等の決定に関するお知らせ"
        self.assertEqual(classify_title(title), {"po_pricing"})

        preliminary = "株式の売出しに係る売出価格の仮条件等の決定に関するお知らせ"
        self.assertIn("po", classify_title(preliminary))
        self.assertNotIn("po_pricing", classify_title(preliminary))

    def test_po_correction_is_classified_separately(self):
        title = "（訂正）当社株式の売出しに関するお知らせの一部訂正"
        self.assertEqual(classify_title(title), {"po_correction"})

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

    def test_yanoshin_rows_are_normalized_once(self):
        payload = '[{"id":"1","title":"株式分割","code":"72030","date":"2026-07-15"}]'.encode("utf-8")
        original = tdnet._normalize_json_disclosure
        with patch("src.collectors.tdnet.request_get", return_value=payload), patch(
            "src.collectors.tdnet._normalize_json_disclosure", wraps=original
        ) as normalize:
            disclosures = tdnet.fetch_yanoshin_disclosures("20260715")
        self.assertEqual(len(disclosures), 1)
        self.assertEqual(disclosures[0].code, "7203")
        self.assertEqual(normalize.call_count, 1)

    def test_real_yanoshin_shape_uses_pdf_id_and_direct_url(self):
        payload = b'''{
          "total_count": 1,
          "items": [{"Tdnet": {
            "id": "1265391",
            "pubdate": "2026-07-14 15:30:00",
            "company_code": "40710",
            "company_name": "\u30d7\u30e9\u30b9\u30a2\u30eb\u30d5\u30a1",
            "title": "\u5f53\u793e\u682a\u5f0f\u306e\u58f2\u51fa\u3057\u306b\u95a2\u3059\u308b\u304a\u77e5\u3089\u305b",
            "document_url": "https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/140120260714592985.pdf"
          }}]
        }'''

        with patch("src.collectors.tdnet.request_get", return_value=payload):
            disclosure = tdnet.fetch_yanoshin_disclosures("20260714")[0]

        self.assertEqual(disclosure.id, "140120260714592985")
        self.assertEqual(disclosure.code, "4071")
        self.assertEqual(disclosure.announced_at.isoformat(), "2026-07-14T15:30:00+09:00")
        self.assertEqual(disclosure.pdf_url, "https://www.release.tdnet.info/inbs/140120260714592985.pdf")

    def test_unknown_yanoshin_schema_raises_for_html_fallback(self):
        with patch("src.collectors.tdnet.request_get", return_value=b'{"unexpected": []}'):
            with self.assertRaises(ValueError):
                tdnet.fetch_yanoshin_disclosures("20260715")

    def test_empty_yanoshin_result_uses_html_fallback(self):
        expected = [object()]
        with patch("src.collectors.tdnet.fetch_yanoshin_disclosures", return_value=[]), patch(
            "src.collectors.tdnet.fetch_tdnet_html_disclosures", return_value=expected
        ) as fallback:
            self.assertIs(tdnet.fetch_disclosures(date(2026, 7, 15)), expected)
        fallback.assert_called_once_with("20260715")

    def test_capped_yanoshin_result_is_merged_with_html_pages(self):
        api_item = tdnet.Disclosure(
            id="api-1",
            code="7203",
            name="A",
            title="株式分割",
            announced_at=tdnet._parse_datetime("202607151500"),
        )
        html_item = tdnet.Disclosure(
            id="html-1",
            code="6758",
            name="B",
            title="株式の売出し",
            announced_at=tdnet._parse_datetime("202607151400"),
        )
        capped = [api_item] * tdnet.YANOSHIN_RESULT_CAP

        with patch("src.collectors.tdnet.fetch_yanoshin_disclosures", return_value=capped), patch(
            "src.collectors.tdnet.fetch_tdnet_html_disclosures", return_value=[api_item, html_item]
        ) as fallback:
            disclosures = tdnet.fetch_disclosures(date(2026, 7, 15))

        self.assertEqual([item.id for item in disclosures], ["api-1", "html-1"])
        fallback.assert_called_once_with("20260715")

    def test_html_fallback_ignores_outer_rows_and_preserves_time(self):
        html = b"""
        <table><tr><td><table>
          <tr><th>\xe6\x99\x82\xe5\x88\xbb</th><th>\xe3\x82\xb3\xe3\x83\xbc\xe3\x83\x89</th><th>\xe4\xbc\x9a\xe7\xa4\xbe\xe5\x90\x8d</th><th>\xe8\xa1\xa8\xe9\xa1\x8c</th></tr>
          <tr><td>15:30</td><td>72030</td><td>\xe3\x83\x86\xe3\x82\xb9\xe3\x83\x88</td><td><a href=\"140120260715000001.pdf\">\xe6\xa0\xaa\xe5\xbc\x8f\xe5\x88\x86\xe5\x89\xb2\xe3\x81\xab\xe9\x96\xa2\xe3\x81\x99\xe3\x82\x8b\xe3\x81\x8a\xe7\x9f\xa5\xe3\x82\x89\xe3\x81\x9b</a></td></tr>
        </table></td></tr></table>
        """
        with patch("src.collectors.tdnet.request_get", return_value=html):
            disclosures = tdnet.fetch_tdnet_html_disclosures("20260715")
        self.assertEqual(len(disclosures), 1)
        self.assertEqual(disclosures[0].code, "7203")
        self.assertEqual(disclosures[0].announced_at.isoformat(), "2026-07-15T15:30:00+09:00")

    def test_html_fallback_fetches_all_pagination_pages(self):
        first = b"""
        <table><tr><td class="pagerTd"><div onclick="pagerLink('I_list_002_20260715.html')">2</div></td></tr></table>
        <table>
          <tr><td>\xe6\x99\x82\xe5\x88\xbb</td><td>\xe3\x82\xb3\xe3\x83\xbc\xe3\x83\x89</td><td>\xe4\xbc\x9a\xe7\xa4\xbe\xe5\x90\x8d</td><td>\xe8\xa1\xa8\xe9\xa1\x8c</td></tr>
          <tr><td>15:30</td><td>72030</td><td>A</td><td><a href="first.pdf">\xe6\xa0\xaa\xe5\xbc\x8f\xe5\x88\x86\xe5\x89\xb2</a></td></tr>
        </table>
        """
        second = b"""
        <table>
          <tr><td>\xe6\x99\x82\xe5\x88\xbb</td><td>\xe3\x82\xb3\xe3\x83\xbc\xe3\x83\x89</td><td>\xe4\xbc\x9a\xe7\xa4\xbe\xe5\x90\x8d</td><td>\xe8\xa1\xa8\xe9\xa1\x8c</td></tr>
          <tr><td>14:00</td><td>67580</td><td>B</td><td><a href="second.pdf">\xe6\xa0\xaa\xe5\xbc\x8f\xe3\x81\xae\xe5\xa3\xb2\xe5\x87\xba\xe3\x81\x97</a></td></tr>
        </table>
        """

        with patch("src.collectors.tdnet.request_get", side_effect=[first, second]) as get:
            disclosures = tdnet.fetch_tdnet_html_disclosures("20260715")

        self.assertEqual([item.code for item in disclosures], ["7203", "6758"])
        self.assertEqual(get.call_count, 2)

    def test_invalid_yanoshin_datetime_is_dropped(self):
        payload = b'[{"id":"1","title":"\xe6\xa0\xaa\xe5\xbc\x8f\xe5\x88\x86\xe5\x89\xb2","code":"72030","date":"bad"}]'
        with patch("src.collectors.tdnet.request_get", return_value=payload):
            self.assertEqual(tdnet.fetch_yanoshin_disclosures("20260715"), [])


if __name__ == "__main__":
    unittest.main()
