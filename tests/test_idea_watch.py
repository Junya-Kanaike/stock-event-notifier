from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import unittest

from src.idea_watch.config import Settings, parse_holdings
from src.idea_watch.extractor import IdeaExtractor, batched, parse_tool_output
from src.idea_watch.http import ApiError
from src.idea_watch.models import ContentItem, Idea, PLATFORM_X, PLATFORM_YOUTUBE, Source
from src.idea_watch.notion_store import NotionStore, build_idea_page, parse_source
from src.idea_watch.pipeline import run_pipeline
from src.idea_watch.x_collector import XCollector
from src.idea_watch.youtube_collector import GeminiVideoDigester, YouTubeCollector, parse_feed


REPO_ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 19, 7, 13, tzinfo=JST)

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <yt:videoId>new1</yt:videoId>
  <title>New video</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=new1"/>
  <author><name>Channel</name></author>
  <published>2026-10-18T12:00:00+00:00</published>
  <media:group><media:description>desc new</media:description></media:group>
 </entry>
 <entry>
  <yt:videoId>old1</yt:videoId>
  <title>Old video</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=old1"/>
  <author><name>Channel</name></author>
  <published>2026-10-10T12:00:00+00:00</published>
  <media:group><media:description>desc old</media:description></media:group>
 </entry>
</feed>"""


def _item(platform=PLATFORM_X, item_id="1", author="A (@a)", text="text"):
    return ContentItem(
        platform=platform,
        item_id=item_id,
        author=author,
        url="https://example.com/" + item_id,
        published_at=NOW,
        text=text,
        text_origin="投稿",
    )


def _idea(item=None, tickers=("285A", "000660.KS"), **overrides):
    values = dict(
        item=item or _item(),
        headline="寄り付き乖離のペア",
        strategy_type="ペア・相対価値",
        direction="ロングショート",
        long_leg="キオクシア 285A",
        short_leg="SKハイニックス 000660.KS",
        tickers=list(tickers),
        trigger="寄り付き時点の騰落率乖離",
        event_date=date(2026, 10, 20),
        entry="寄り付き",
        exit="大引け",
        mechanism="連動銘柄の一時的な乖離が引けにかけて縮小",
        validation="過去1年の乖離上位日で寄り→引けのペアリターンを集計",
        reproducibility="高",
        horizon="日計り",
        caveats="キオクシアは貸借銘柄か確認",
        summary="要約",
    )
    values.update(overrides)
    return Idea(**values)


SAMPLE_RAW = {
    "source_index": 0,
    "headline": "h",
    "strategy_type": "指数・リバランス",
    "direction": "ショート",
    "long_leg": "",
    "short_leg": "キオクシア",
    "tickers": ["285a"],
    "trigger": "TOPIXウエイト低減",
    "event_date": "2027-01-29",
    "entry": "発表翌日寄り付き",
    "exit": "実施日大引け",
    "mechanism": "m",
    "validation": "v",
    "reproducibility": "超高",
    "horizon": "数週間",
    "caveats": "c",
    "summary": "s",
}


class FakeTranscripts:
    def __init__(self, text=None):
        self.text = text
        self.blocked = text is None

    def fetch(self, video_id):
        return self.text


class ConfigTest(unittest.TestCase):
    def test_parse_holdings_accepts_codes_names_and_pairs(self):
        self.assertEqual(
            parse_holdings("285A:キオクシア, 6857 ,レーザーテック、"),
            [("285A", "キオクシア"), ("6857", ""), ("", "レーザーテック")],
        )

    def test_missing_required_settings_raise(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                Settings.from_env()


class NotionTest(unittest.TestCase):
    def test_parse_source_strips_at_and_skips_incomplete_rows(self):
        page = {
            "id": "p1",
            "properties": {
                "名前": {"type": "title", "title": [{"plain_text": "誰か"}]},
                "プラットフォーム": {"type": "select", "select": {"name": "X"}},
                "ハンドル": {"type": "rich_text", "rich_text": [{"plain_text": "@someone"}]},
                "内部ID": {"type": "rich_text", "rich_text": []},
                "カーソル": {"type": "rich_text", "rich_text": [{"plain_text": "123"}]},
            },
        }
        source = parse_source(page)
        self.assertEqual((source.handle, source.internal_id, source.cursor), ("someone", None, "123"))
        page["properties"]["ハンドル"]["rich_text"] = []
        self.assertIsNone(parse_source(page))

    def test_idea_page_uses_data_source_parent_and_dedupe_key(self):
        idea = _idea(item=_item(item_id="999"), holding_related=True)
        page = build_idea_page("ds-ideas", idea)
        props = page["properties"]
        self.assertEqual(page["parent"], {"type": "data_source_id", "data_source_id": "ds-ideas"})
        key = props["ソースID"]["rich_text"][0]["text"]["content"]
        self.assertTrue(key.startswith("x:999:"))
        # 同じ投稿・同じ組み合わせなら同じキー（重複防止）、銘柄が違えば別キー
        self.assertEqual(key, _idea(item=_item(item_id="999"), tickers=("000660.KS", "285A")).source_key)
        self.assertNotEqual(key, _idea(item=_item(item_id="999"), tickers=("6857",)).source_key)
        self.assertEqual(props["戦略タイプ"], {"select": {"name": "ペア・相対価値"}})
        self.assertEqual(props["予定日"], {"date": {"start": "2026-10-20"}})
        self.assertEqual(props["銘柄コード"]["rich_text"][0]["text"]["content"], "285A, 000660.KS")
        self.assertTrue(props["保有銘柄関連"]["checkbox"])
        self.assertEqual(build_idea_page("d", _idea(event_date=None))["properties"]["予定日"], {"date": None})

    def test_parse_source_accepts_search_query_with_at_sign(self):
        page = {
            "id": "p2",
            "properties": {
                "名前": {"type": "title", "title": [{"plain_text": "リバランス"}]},
                "プラットフォーム": {"type": "select", "select": {"name": "X検索"}},
                "ハンドル": {"type": "rich_text", "rich_text": [{"plain_text": "リバランス 空売り from:@x"}]},
            },
        }
        self.assertEqual(parse_source(page).handle, "リバランス 空売り from:@x")

    def test_store_queries_data_source_endpoint_with_version_header(self):
        calls = []

        def requester(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return {"results": [], "has_more": False}

        store = NotionStore("tok", "ds-src", "ds-ideas", requester=requester)
        self.assertEqual(store.list_sources(), [])
        method, url, kwargs = calls[0]
        self.assertEqual((method, url), ("POST", "https://api.notion.com/v1/data_sources/ds-src/query"))
        self.assertEqual(kwargs["headers"]["Notion-Version"], "2025-09-03")


class XCollectorTest(unittest.TestCase):
    def test_first_run_uses_lookback_and_respects_run_budget(self):
        calls = []

        def requester(method, url, **kwargs):
            calls.append(kwargs["params"].copy())
            posts = [
                {"id": str(100 + i), "text": f"t{i}", "created_at": "2026-10-18T10:00:00.000Z"}
                for i in range(5)
            ]
            return {"data": posts, "meta": {"newest_id": "104", "next_token": "nx"}}

        collector = XCollector("tok", max_posts_per_source=20, max_posts_per_run=7, requester=requester)
        source = Source("p", "名前", "X", "handle", internal_id="42")
        items, cursor = collector.fetch(source, now=NOW)
        self.assertIn("start_time", calls[0])
        self.assertNotIn("since_id", calls[0])
        self.assertEqual(calls[0]["exclude"], "retweets,replies")
        self.assertEqual(len(items), 7)
        self.assertEqual(cursor, "104")
        # APIの最小max_resultsが5のため、上限超過は最大4件に収まる
        self.assertEqual(collector.posts_read, 10)
        self.assertLessEqual(collector.remaining_budget, 0)
        self.assertEqual(items[0].url, "https://x.com/handle/status/100")

    def test_since_id_and_long_post_text(self):
        def requester(method, url, **kwargs):
            self.assertEqual(kwargs["params"]["since_id"], "500")
            return {
                "data": [{"id": "501", "text": "short", "note_tweet": {"text": "long body"}}],
                "meta": {"newest_id": "501"},
            }

        collector = XCollector("tok", requester=requester)
        items, cursor = collector.fetch(Source("p", "n", "X", "h", internal_id="1", cursor="500"), now=NOW)
        self.assertEqual(items[0].text, "long body")
        self.assertEqual(cursor, "501")

    def test_no_budget_means_no_api_call(self):
        def requester(*args, **kwargs):
            raise AssertionError("should not be called")

        collector = XCollector("tok", max_posts_per_run=0, requester=requester)
        self.assertEqual(collector.fetch(Source("p", "n", "X", "h", internal_id="1"), now=NOW), ([], None))

    def test_search_uses_recent_endpoint_without_user_expansion(self):
        calls = []

        def requester(method, url, **kwargs):
            calls.append((url, kwargs["params"].copy()))
            if len(calls) == 1:
                return {"data": [{"id": "900", "text": "a"}], "meta": {"newest_id": "900", "next_token": "nt"}}
            return {"data": [{"id": "899", "text": "b"}], "meta": {}}

        collector = XCollector("tok", requester=requester)
        items, cursor = collector.fetch_search(Source("p", "リバランス", "X検索", "リバランス 空売り"), now=NOW)
        url, params = calls[0]
        self.assertTrue(url.endswith("/tweets/search/recent"))
        self.assertEqual(params["query"], "リバランス 空売り -is:retweet")
        self.assertNotIn("expansions", params)
        self.assertGreaterEqual(params["max_results"], 10)
        self.assertEqual(calls[1][1]["next_token"], "nt")
        self.assertEqual(cursor, "900")
        self.assertEqual(items[0].platform, "X")
        self.assertEqual(items[0].url, "https://x.com/i/web/status/900")
        self.assertEqual(items[0].author, "X検索「リバランス」")


class YouTubeCollectorTest(unittest.TestCase):
    def test_parse_feed(self):
        entries = parse_feed(FEED)
        self.assertEqual([e["video_id"] for e in entries], ["new1", "old1"])
        self.assertEqual(entries[0]["description"], "desc new")

    def test_only_new_videos_and_description_fallback(self):
        collector = YouTubeCollector(transcript_fetcher=FakeTranscripts(None), feed_fetcher=lambda cid: FEED)
        items, cursor = collector.fetch(Source("p", "ch", "YouTube", "UCxxx"), now=NOW)
        self.assertEqual([i.item_id for i in items], ["new1"])
        self.assertEqual(items[0].text_origin, "概要欄のみ")
        self.assertEqual(items[0].text, "desc new")
        self.assertTrue(cursor.startswith("2026-10-18T12:00:00"))

    def test_cursor_excludes_already_seen_and_transcript_is_used(self):
        collector = YouTubeCollector(transcript_fetcher=FakeTranscripts("字幕本文"), feed_fetcher=lambda cid: FEED)
        items, _ = collector.fetch(Source("p", "ch", "YouTube", "UCxxx", cursor="2026-10-01T00:00:00+00:00"), now=NOW)
        self.assertEqual([i.item_id for i in items], ["old1", "new1"])
        self.assertTrue(all(i.text_origin == "字幕" for i in items))
        items, cursor = collector.fetch(
            Source("p", "ch", "YouTube", "UCxxx", cursor="2026-10-18T12:00:00+00:00"), now=NOW
        )
        self.assertEqual((items, cursor), ([], None))

    def test_gemini_is_preferred_and_falls_back_on_failure(self):
        sent = []

        def ok(method, url, **kwargs):
            sent.append((url, kwargs))
            return {
                "candidates": [{"content": {"parts": [{"text": "動画の要約"}]}}],
                "usageMetadata": {"promptTokenCount": 1234},
            }

        gemini = GeminiVideoDigester("gkey", "gemini-test", requester=ok)
        collector = YouTubeCollector(
            transcript_fetcher=FakeTranscripts("字幕"), gemini=gemini, feed_fetcher=lambda cid: FEED
        )
        items, _ = collector.fetch(Source("p", "ch", "YouTube", "UCxxx"), now=NOW)
        self.assertEqual((items[0].text, items[0].text_origin), ("動画の要約", "動画(Gemini)"))
        url, kwargs = sent[0]
        self.assertIn("/models/gemini-test:generateContent", url)
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "gkey")
        parts = kwargs["json_body"]["contents"][0]["parts"]
        self.assertEqual(parts[1], {"file_data": {"file_uri": "https://www.youtube.com/watch?v=new1"}})
        self.assertEqual(gemini.input_tokens, 1234)

        def fail(*args, **kwargs):
            raise ApiError("Gemini", 400)

        broken = GeminiVideoDigester("gkey", requester=fail)
        collector = YouTubeCollector(transcript_fetcher=FakeTranscripts("字幕"), gemini=broken, feed_fetcher=lambda cid: FEED)
        items, _ = collector.fetch(Source("p", "ch", "YouTube", "UCxxx"), now=NOW)
        self.assertEqual(items[0].text_origin, "字幕")
        self.assertEqual(broken.failures, 1)


class ExtractorTest(unittest.TestCase):
    def test_batches_x_by_author_and_videos_individually(self):
        items = [_item(item_id=str(i), author="A") for i in range(25)] + [
            _item(PLATFORM_YOUTUBE, "v1", "C"),
            _item(item_id="b", author="B"),
        ]
        sizes = sorted(len(b) for b in batched(items))
        self.assertEqual(sizes, [1, 1, 5, 20])

    def test_tool_output_is_normalized_filtered_and_holdings_flagged(self):
        response = {
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [
                {
                    "type": "tool_use",
                    "name": "record_ideas",
                    "input": {
                        "ideas": [
                            dict(SAMPLE_RAW),
                            dict(SAMPLE_RAW, source_index=9),  # 範囲外
                            dict(SAMPLE_RAW, strategy_type="業績予想"),  # やり方に合わない分類
                            dict(SAMPLE_RAW, trigger=""),  # トリガーなし
                            dict(SAMPLE_RAW, long_leg="", short_leg="", tickers=[]),  # 対象なし
                        ]
                    },
                }
            ],
        }
        sent = []

        def requester(method, url, **kwargs):
            sent.append(kwargs["json_body"])
            return response

        extractor = IdeaExtractor("key", "model-x", holdings=[("285A", "キオクシア")], requester=requester)
        ideas = extractor.extract([_item()])
        self.assertEqual(len(ideas), 1)
        idea = ideas[0]
        self.assertEqual(idea.tickers, ["285A"])
        self.assertEqual(idea.event_date, date(2027, 1, 29))
        self.assertEqual(idea.reproducibility, "低")  # enum外は既定値
        self.assertTrue(idea.holding_related)
        self.assertEqual(sent[0]["tool_choice"], {"type": "tool", "name": "record_ideas"})
        self.assertEqual(sent[0]["model"], "model-x")
        self.assertEqual(extractor.input_tokens, 10)

    def test_prompt_encodes_the_investors_style(self):
        from src.idea_watch.extractor import SYSTEM_PROMPT

        for phrase in ["受注残", "決算プレー", "ロングショート", "寄り付き", "日経VI", "過去の同種事例"]:
            self.assertIn(phrase, SYSTEM_PROMPT)

    def test_parse_tool_output_without_tool_block(self):
        self.assertEqual(parse_tool_output({"content": [{"type": "text", "text": "x"}]}), [])

    def test_empty_items_skip_api(self):
        def requester(*args, **kwargs):
            raise AssertionError("should not be called")

        extractor = IdeaExtractor("key", "m", requester=requester)
        self.assertEqual(extractor.extract([_item(text="  ")]), [])


class FakeStore:
    def __init__(self, sources, existing=()):
        self.sources = sources
        self.existing = set(existing)
        self.created = []
        self.updates = []

    def list_sources(self):
        return self.sources

    def update_source(self, source, **kwargs):
        self.updates.append((source.page_id, kwargs))

    def idea_exists(self, key):
        return key in self.existing

    def create_idea(self, idea):
        self.created.append(idea.source_key)


class FakeExtractor:
    def extract(self, items):
        return [_idea(item=item, tickers=("6857",)) for item in items]


class FakeCollector:
    def __init__(self, items=None, cursor="c2", error=None):
        self.items = items or []
        self.cursor = cursor
        self.error = error
        self.resolved = []

    def resolve_user_id(self, handle):
        self.resolved.append(handle)
        return "uid"

    def fetch(self, source, *, now):
        if self.error:
            raise self.error
        return self.items, self.cursor


class PipelineTest(unittest.TestCase):
    def test_writes_new_ideas_skips_duplicates_and_advances_cursor(self):
        src = Source("p1", "n", "X", "h")
        store = FakeStore([src], existing={_idea(item=_item(item_id="2"), tickers=("6857",)).source_key})
        x = FakeCollector([_item(item_id="1"), _item(item_id="2")], cursor="2")
        report = run_pipeline(
            store=store, extractor=FakeExtractor(), x_collector=x, youtube_collector=FakeCollector(), now=NOW
        )
        self.assertEqual(store.created, [_idea(item=_item(item_id="1"), tickers=("6857",)).source_key])
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(x.resolved, ["h"])
        self.assertEqual(store.updates[0], ("p1", {"internal_id": "uid"}))
        self.assertEqual(store.updates[1], ("p1", {"cursor": "2", "fetched_at": NOW}))

    def test_failure_does_not_advance_cursor_or_leak_urls(self):
        import requests

        err = requests.HTTPError("404 for url https://www.youtube.com/feeds/videos.xml?channel_id=UCsecret")
        store = FakeStore([Source("p1", "n", "YouTube", "UCsecret"), Source("p2", "n", "X", "h", internal_id="1")])
        report = run_pipeline(
            store=store,
            extractor=FakeExtractor(),
            x_collector=FakeCollector([], cursor=None),
            youtube_collector=FakeCollector(error=err),
            now=NOW,
        )
        self.assertEqual(len(report.failures), 1)
        self.assertNotIn("UCsecret", report.failures[0])
        self.assertEqual([u[0] for u in store.updates], ["p2"])
        api = run_pipeline(
            store=FakeStore([Source("p3", "n", "YouTube", "UC")]),
            extractor=FakeExtractor(),
            x_collector=None,
            youtube_collector=FakeCollector(error=ApiError("Notion", 401, "secret body")),
            now=NOW,
        )
        self.assertEqual(api.failures, ["YouTube#1: Notion API error: HTTP 401"])

    def test_dry_run_writes_nothing(self):
        store = FakeStore([Source("p1", "n", "YouTube", "UC")])
        report = run_pipeline(
            store=store,
            extractor=FakeExtractor(),
            x_collector=None,
            youtube_collector=FakeCollector([_item(PLATFORM_YOUTUBE, "v")]),
            now=NOW,
            dry_run=True,
        )
        self.assertEqual((store.created, store.updates, len(report.printed)), ([], [], 1))

    def test_x_without_token_is_skipped(self):
        report = run_pipeline(
            store=FakeStore([Source("p1", "n", "X", "h")]),
            extractor=FakeExtractor(),
            x_collector=None,
            youtube_collector=FakeCollector(),
            now=NOW,
        )
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(report.failures, [])


class IdeaWorkflowTest(unittest.TestCase):
    def test_workflow_is_opt_in_separate_lock_read_only_and_never_dry_run(self):
        workflow = (REPO_ROOT / ".github/workflows/idea_watch.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "13 22 * * *"', workflow)
        self.assertIn('cron: "13 7 * * *"', workflow)
        self.assertIn("vars.IDEA_WATCH_ENABLED == 'true'", workflow)
        self.assertIn("group: idea-watch", workflow)
        self.assertNotIn("group: event-state-", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("commit_state", workflow)
        self.assertIn("python -m src.run_ideas\n", workflow)
        self.assertNotIn("run: python -m src.run_ideas --dry-run", workflow)


class PipelineSearchTest(unittest.TestCase):
    def test_x_search_source_routes_to_search_without_user_lookup(self):
        class SearchCollector(FakeCollector):
            def __init__(self):
                super().__init__()
                self.searched = []

            def fetch_search(self, source, *, now):
                self.searched.append(source.handle)
                return [_item(item_id="s1")], "s1"

        x = SearchCollector()
        store = FakeStore([Source("p1", "q", "X検索", "TOPIX 除外")])
        run_pipeline(store=store, extractor=FakeExtractor(), x_collector=x, youtube_collector=FakeCollector(), now=NOW)
        self.assertEqual((x.searched, x.resolved), (["TOPIX 除外"], []))
        self.assertEqual(store.updates, [("p1", {"cursor": "s1", "fetched_at": NOW})])


if __name__ == "__main__":
    unittest.main()
