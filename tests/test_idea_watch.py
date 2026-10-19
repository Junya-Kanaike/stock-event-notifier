from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from src.idea_watch.config import Settings, parse_holdings
from src.idea_watch.extractor import IdeaExtractor, batched, parse_tool_output
from src.idea_watch.http import ApiError
from src.idea_watch.models import ContentItem, Idea, PLATFORM_X, PLATFORM_YOUTUBE, Source
from src.idea_watch.notion_store import NotionStore, build_idea_page, parse_source
from src.idea_watch.pipeline import run_pipeline
from src.idea_watch.x_collector import XCollector
from src.idea_watch.youtube_collector import YouTubeCollector, parse_feed


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
        idea = Idea(
            item=_item(item_id="999"),
            headline="見出し",
            ticker="6857",
            company="アドバンテスト",
            market="日本株",
            direction="ロング",
            themes=["AI半導体", "テスタ,需要"],
            thesis="根拠",
            catalyst="決算",
            horizon="中期",
            evidence="高",
            summary="要約",
            holding_related=True,
        )
        page = build_idea_page("ds-ideas", idea)
        self.assertEqual(page["parent"], {"type": "data_source_id", "data_source_id": "ds-ideas"})
        self.assertEqual(page["properties"]["ソースID"]["rich_text"][0]["text"]["content"], "x:999:6857")
        self.assertEqual(page["properties"]["テーマ"]["multi_select"][1]["name"], "テスタ・需要")
        self.assertTrue(page["properties"]["保有銘柄関連"]["checkbox"])

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


class ExtractorTest(unittest.TestCase):
    def test_batches_x_by_author_and_videos_individually(self):
        items = [_item(item_id=str(i), author="A") for i in range(25)] + [
            _item(PLATFORM_YOUTUBE, "v1", "C"),
            _item(item_id="b", author="B"),
        ]
        sizes = sorted(len(b) for b in batched(items))
        self.assertEqual(sizes, [1, 1, 5, 20])

    def test_tool_output_is_normalized_and_holdings_flagged(self):
        response = {
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [
                {
                    "type": "tool_use",
                    "name": "record_ideas",
                    "input": {
                        "ideas": [
                            {
                                "source_index": 0,
                                "headline": "h",
                                "ticker": "285a",
                                "company": "キオクシア",
                                "market": "日本株",
                                "direction": "強気",
                                "themes": ["NAND"],
                                "thesis": "t",
                                "catalyst": "",
                                "horizon": "短期",
                                "evidence": "中",
                                "summary": "s",
                            },
                            {"source_index": 9, "ticker": "7203", "company": "トヨタ"},
                            {"source_index": 0, "ticker": "", "company": ""},
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
        self.assertEqual(ideas[0].ticker, "285A")
        self.assertEqual(ideas[0].direction, "ウォッチ")  # enum外は既定値
        self.assertTrue(ideas[0].holding_related)
        self.assertEqual(sent[0]["tool_choice"], {"type": "tool", "name": "record_ideas"})
        self.assertEqual(sent[0]["model"], "model-x")
        self.assertEqual(extractor.input_tokens, 10)

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
        return [
            Idea(item, "h", "6857", "c", "日本株", "ロング", [], "t", "", "中期", "中", "s")
            for item in items
        ]


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
        store = FakeStore([src], existing={"x:2:6857"})
        x = FakeCollector([_item(item_id="1"), _item(item_id="2")], cursor="2")
        report = run_pipeline(
            store=store, extractor=FakeExtractor(), x_collector=x, youtube_collector=FakeCollector(), now=NOW
        )
        self.assertEqual(store.created, ["x:1:6857"])
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


if __name__ == "__main__":
    unittest.main()
