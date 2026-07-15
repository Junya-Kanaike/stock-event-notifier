import unittest

from src.collectors.jpx_bunbai import parse_bunbai_html
from src.collectors.jpx_ipo import parse_ipo_html
from src.collectors.jpx_margin import find_margin_excel_url


class CollectorParserTest(unittest.TestCase):
    def test_ipo_parser_keeps_multiple_companies_on_the_same_date(self):
        html = """
        <table>
          <tr><th>上場日</th><th>会社名</th><th>コード</th><th>市場区分</th></tr>
          <tr><td>2026/07/29</td><td>アイ・グリッド</td><td>603A</td><td>グロース</td></tr>
          <tr><td>2026/07/29</td><td>ビーエイブル</td><td>604A</td><td>スタンダード</td></tr>
          <tr><td>2026/07/15</td><td>チャットプラス</td><td>598A</td><td>グロース</td></tr>
        </table>
        """
        records = parse_ipo_html(html, default_year=2026)
        self.assertEqual([item["code"] for item in records], ["603A", "604A", "598A"])
        self.assertEqual(records[0]["market"], "グロース")
        self.assertEqual(records[1]["listing_date"], "2026-07-29")

    def test_ipo_parser_reads_market_from_jpx_second_row(self):
        html = """
        <table>
          <tr><th>上場日</th><th>会社名</th><th>コード</th><th>売買単位</th></tr>
          <tr><th>市場区分</th><th>Iの部</th></tr>
          <tr><td>2026/08/04 （2026/06/30）</td><td>エブリー</td><td>607A</td><td>100</td></tr>
          <tr><td>グロース</td><td></td></tr>
        </table>
        """
        records = parse_ipo_html(html, default_year=2026)
        self.assertEqual(records[0]["market"], "グロース")

    def test_bunbai_parser_uses_named_columns(self):
        html = """
        <table>
          <tr><th>実施日</th><th>銘柄名（コード）</th><th>終値</th></tr>
          <tr><td>2026/07/21</td><td>テスト株式会社 株式（7203）</td><td>1,000円</td></tr>
        </table>
        """
        records = parse_bunbai_html(html, default_year=2026)
        self.assertEqual(records[0]["code"], "7203")
        self.assertEqual(records[0]["name"], "テスト株式会社")
        self.assertEqual(records[0]["execution_date"], "2026-07-21")

    def test_margin_excel_link_uses_surrounding_label(self):
        html = """
        <p>制度信用・貸借銘柄一覧 <a href="/files/margin.xlsx"><img alt="Excel"></a></p>
        """
        self.assertEqual(
            find_margin_excel_url(html, "https://www.jpx.co.jp/listing/others/margin/index.html"),
            "https://www.jpx.co.jp/files/margin.xlsx",
        )


if __name__ == "__main__":
    unittest.main()
