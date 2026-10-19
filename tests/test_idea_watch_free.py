"""追加料金なし構成（Gemini無料枠・受信箱）のテスト。"""
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest import mock

from src.idea_watch.config import Settings
from src.idea_watch.gemini_extractor import (
    GeminiIdeaExtractor,
    QuotaExhausted,
    RESPONSE_SCHEMA,
    parse_gemini_ideas,
)
from src.idea_watch.http import ApiError
from src.idea_watch.inbox import InboxEntry, InboxFetchError, InboxItemBuilder, classify_url, parse_oembed_text
from src.idea_watch.models import ContentItem, Source
from src.idea_watch.notion_store import parse_inbox_entry
from src.idea_watch.pipeline import fit_to_budget, run_pipeline
from src.idea_watch.youtube_collector import YouTubeCollector
from tests.test_idea_watch import FEED, NOW, SAMPLE_RAW, FakeCollector, FakeStore, _idea, _item


JST = timezone(timedelta(hours=9))


def gemini_response(ideas):
    return {
        "candidates": [{"content": {"parts": [{"text": json.dumps({"ideas": ideas}, ensure_ascii=False)}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
    }


def video_item(video_id="vid00000001", minutes=0):
    item = _item(platform="YouTube", item_id=video_id, text="概要欄")
    item.video_url = f"https://www.youtube.com/watch?v={video_id}"
    item.published_at = NOW + timedelta(minutes=minutes)
    return item


class GeminiExtractorTest(unittest.TestCase):
    def make(self, responder, **kwargs):
        self.sent = []
        self.slept = []

        def requester(method, url, **kw):
            self.sent.append((url, kw))
            return responder(len(self.sent))

        clock = iter(range(0, 1000, 1))
        return GeminiIdeaExtractor(
            "gkey",
            "gemini-x",
            requester=requester,
            sleeper=self.slept.append,
            clock=lambda: float(next(clock)),
            **kwargs,
        )

    def test_video_is_watched_and_extracted_in_one_request(self):
        extractor = self.make(lambda n: gemini_response([SAMPLE_RAW]))
        ideas = extractor.extract([video_item()])
        self.assertEqual(len(ideas), 1)
        url, kw = self.sent[0]
        self.assertIn("/models/gemini-x:generateContent", url)
        body = kw["json_body"]
        self.assertIn("受注残", body["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(body["contents"][0]["parts"][1], {"file_data": {"file_uri": video_item().video_url}})
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(body["generationConfig"]["responseSchema"], RESPONSE_SCHEMA)
        self.assertEqual(extractor.remaining_requests, 8)

    def test_texts_are_batched_across_authors_and_requests_are_paced(self):
        extractor = self.make(lambda n: gemini_response([]), min_interval_sec=13)
        items = [_item(item_id=str(i), author=f"A{i % 3}") for i in range(25)] + [video_item()]
        self.assertEqual(extractor.requests_needed(items), 3)  # 動画1 + テキスト20件 + 5件
        extractor.extract(items)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len(self.slept), 2)
        self.assertTrue(all(wait > 0 for wait in self.slept))

    def test_budget_and_429_raise_quota_exhausted(self):
        extractor = self.make(lambda n: gemini_response([]), max_requests=1)
        extractor.extract([_item()])
        with self.assertRaises(QuotaExhausted):
            extractor.extract([_item(item_id="2")])

        def limited(n):
            raise ApiError("Gemini", 429)

        extractor = self.make(limited)
        with self.assertRaises(QuotaExhausted):
            extractor.extract([_item()])
        self.assertEqual(extractor.remaining_requests, 0)

    def test_schema_is_converted_to_gemini_types(self):
        item_schema = RESPONSE_SCHEMA["properties"]["ideas"]["items"]
        self.assertEqual(RESPONSE_SCHEMA["type"], "OBJECT")
        self.assertEqual(item_schema["properties"]["tickers"]["type"], "ARRAY")
        self.assertIn("ペア・相対価値", item_schema["properties"]["strategy_type"]["enum"])

    def test_parse_tolerates_wrapped_json(self):
        data = {"candidates": [{"content": {"parts": [{"text": 'ok ```{"ideas": [{"a": 1}]}```'}]}}]}
        self.assertEqual(parse_gemini_ideas(data), [{"a": 1}])
        self.assertEqual(parse_gemini_ideas({"candidates": []}), [])


class BudgetTest(unittest.TestCase):
    def test_fit_keeps_oldest_items_within_budget(self):
        extractor = GeminiIdeaExtractor("k", max_requests=2)
        items = [video_item("vid0000000" + str(i), minutes=i) for i in (3, 1, 2)]
        kept = fit_to_budget(items, extractor)
        self.assertEqual([i.item_id for i in kept], ["vid00000001", "vid00000002"])

    def test_pipeline_advances_cursor_only_to_last_processed_video(self):
        class Extractor:
            remaining_requests = 1

            @staticmethod
            def requests_needed(items):
                return len(items)

            def extract(self, items):
                self.remaining_requests = 0
                return []

        items = [video_item("vid00000001", 0), video_item("vid00000002", 5)]
        store = FakeStore([Source("p1", "ch", "YouTube", "UC"), Source("p2", "ch2", "YouTube", "UC2")])
        report = run_pipeline(
            store=store,
            extractor=Extractor(),
            x_collector=None,
            youtube_collector=FakeCollector(items, cursor="latest"),
            now=NOW,
        )
        self.assertEqual(store.updates[0], ("p1", {"cursor": items[0].published_at.isoformat(), "fetched_at": NOW}))
        self.assertEqual(len(store.updates), 1)  # 2つ目のソースは無料枠切れで据え置き
        self.assertEqual(report.failures, [])
        self.assertEqual(len(report.skipped), 2)


class InboxTest(unittest.TestCase):
    def entry(self, url=None, title="", memo=""):
        return InboxEntry("abc-123", title, url, memo, NOW)

    def test_classify_urls(self):
        self.assertEqual(classify_url("https://x.com/foo/status/123?s=20"), ("X", "123"))
        self.assertEqual(classify_url("https://twitter.com/foo/status/456"), ("X", "456"))
        self.assertEqual(classify_url("https://youtu.be/abcdefghijk?t=3"), ("YouTube", "abcdefghijk"))
        self.assertEqual(classify_url("https://www.youtube.com/watch?si=x&v=abcdefghijk"), ("YouTube", "abcdefghijk"))
        self.assertEqual(classify_url("https://www.youtube.com/shorts/abcdefghijk"), ("YouTube", "abcdefghijk"))
        self.assertEqual(classify_url("https://note.com/a/n/1"), ("メモ", None))
        self.assertEqual(classify_url(None), ("メモ", None))

    def test_x_post_is_read_via_oembed_with_memo(self):
        payload = {
            "author_name": "イベント投資家",
            "html": '<blockquote class="twitter-tweet"><p lang="ja">受渡日の寄りで<br>買い戻し</p>&mdash; x <a href="#">Oct 18</a></blockquote>',
        }
        builder = InboxItemBuilder(youtube_item_factory=None, oembed_fetcher=lambda url: payload)
        item = builder.build(self.entry(title="共有: https://x.com/foo/status/789", memo="次のPOで試す"))
        self.assertEqual((item.platform, item.item_id), ("X", "789"))
        self.assertIn("受渡日の寄りで\n買い戻し", item.text)
        self.assertIn("保存者のメモ: 次のPOで試す", item.text)
        self.assertEqual(item.author, "イベント投資家（受信箱）")
        self.assertEqual(parse_oembed_text({"html": ""}), "")

    def test_youtube_and_memo_entries(self):
        made = []
        builder = InboxItemBuilder(youtube_item_factory=lambda vid, entry: made.append(vid) or video_item(vid))
        builder.build(self.entry(url="https://youtu.be/abcdefghijk"))
        self.assertEqual(made, ["abcdefghijk"])
        memo = builder.build(self.entry(title="SQ週の寄りの癖", memo="先物主導で現物が遅れる"))
        self.assertEqual((memo.platform, memo.item_id), ("メモ", "abc123"))
        with self.assertRaises(InboxFetchError):
            builder.build(self.entry())

    def test_parse_inbox_entry_from_notion(self):
        page = {
            "id": "pg",
            "created_time": "2026-10-19T03:00:00.000Z",
            "properties": {
                "タイトル": {"type": "title", "title": [{"plain_text": "t"}]},
                "URL": {"type": "url", "url": "https://x.com/a/status/1"},
                "メモ": {"type": "rich_text", "rich_text": [{"plain_text": "m"}]},
            },
        }
        entry = parse_inbox_entry(page)
        self.assertEqual((entry.title, entry.url, entry.memo), ("t", "https://x.com/a/status/1", "m"))
        self.assertEqual(entry.created_at.tzinfo, timezone.utc)


class InboxStore(FakeStore):
    def __init__(self, entries):
        super().__init__([])
        self.entries = entries
        self.marks = []

    def list_inbox(self, ds):
        return self.entries

    def mark_inbox(self, page_id, status, result):
        self.marks.append((page_id, status, result))


class InboxBuilderStub:
    def build(self, entry):
        if entry.title == "bad":
            raise InboxFetchError("oEmbed HTTP 404")
        return _item(item_id=entry.page_id, text=entry.title)


class InboxExtractor:
    remaining_requests = None

    def extract(self, items):
        return [_idea(item=items[0], tickers=("7012",))]


class PipelineInboxTest(unittest.TestCase):
    def test_inbox_rows_are_extracted_and_marked(self):
        entries = [
            InboxEntry("e1", "good", None, "", NOW),
            InboxEntry("e2", "other", None, "", NOW),
            InboxEntry("e3", "bad", None, "", NOW),
        ]
        store = InboxStore(entries)
        report = run_pipeline(
            store=store,
            extractor=InboxExtractor(),
            x_collector=None,
            youtube_collector=FakeCollector(),
            now=NOW,
            inbox_builder=InboxBuilderStub(),
            inbox_data_source_id="ds-inbox",
        )
        self.assertEqual(len(store.created), 1)
        self.assertIn(("e1", "処理済み", "アイデア1件を追加"), store.marks)
        self.assertIn(("e2", "処理済み", "該当なし（やり方に合う戦略は見つからず）"), store.marks)
        self.assertIn(("e3", "取得失敗", "本文を取得できませんでした（oEmbed HTTP 404）"), store.marks)
        self.assertEqual((report.inbox_processed, report.inbox_failed), (2, 1))

    def test_inbox_rows_stay_unprocessed_when_quota_runs_out(self):
        class Broke:
            remaining_requests = 5

            @staticmethod
            def requests_needed(items):
                return 1

            def extract(self, items):
                raise QuotaExhausted("429")

        store = InboxStore([InboxEntry("e1", "good", None, "", NOW)])
        report = run_pipeline(
            store=store,
            extractor=Broke(),
            x_collector=None,
            youtube_collector=FakeCollector(),
            now=NOW,
            inbox_builder=InboxBuilderStub(),
            inbox_data_source_id="ds",
        )
        self.assertEqual(store.marks, [])
        self.assertEqual(report.failures, [])


class FreeConfigTest(unittest.TestCase):
    base = {
        "NOTION_TOKEN": "n",
        "IDEA_SOURCES_DATA_SOURCE_ID": "s",
        "IDEA_IDEAS_DATA_SOURCE_ID": "i",
    }

    def test_gemini_only_is_enough_and_is_default(self):
        with mock.patch.dict(os.environ, dict(self.base, GEMINI_API_KEY="g"), clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.llm, settings.anthropic_api_key, settings.gemini_max_requests), ("gemini", None, 9))

    def test_explicit_claude_requires_key(self):
        with mock.patch.dict(os.environ, dict(self.base, GEMINI_API_KEY="g", IDEA_LLM="claude"), clear=True):
            with self.assertRaises(RuntimeError):
                Settings.from_env()
        with mock.patch.dict(os.environ, dict(self.base, ANTHROPIC_API_KEY="a"), clear=True):
            self.assertEqual(Settings.from_env().llm, "claude")


class DeferredYouTubeTest(unittest.TestCase):
    def test_deferred_mode_passes_video_url_without_fetching_transcripts(self):
        class NoTranscripts:
            blocked = False

            def fetch(self, video_id):
                raise AssertionError("should not fetch transcripts")

        collector = YouTubeCollector(
            transcript_fetcher=NoTranscripts(), defer_video_to_extractor=True, feed_fetcher=lambda cid: FEED
        )
        items, _ = collector.fetch(Source("p", "ch", "YouTube", "UC"), now=NOW)
        self.assertEqual(items[0].video_url, "https://www.youtube.com/watch?v=new1")
        self.assertEqual(items[0].text_origin, "動画(Gemini)")


if __name__ == "__main__":
    unittest.main()
