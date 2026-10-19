from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from src.idea_watch.models import ContentItem, PLATFORM_YOUTUBE, Source


FEED_URL = "https://www.youtube.com/feeds/videos.xml"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
TRANSCRIPT_LANGUAGES = ["ja", "en"]


def fetch_feed_xml(channel_id: str) -> bytes:
    response = requests.get(
        FEED_URL,
        params={"channel_id": channel_id},
        headers={"User-Agent": "Mozilla/5.0 (personal idea watcher)"},
        timeout=20,
    )
    response.raise_for_status()
    return response.content


def parse_feed(xml_bytes: bytes) -> list[dict[str, Any]]:
    # 外部XMLなので defusedxml（youtube-transcript-api の依存）で安全にパースする
    from defusedxml.ElementTree import fromstring

    root = fromstring(xml_bytes)
    entries: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=NS)
        published_raw = entry.findtext("atom:published", default="", namespaces=NS)
        if not video_id or not published_raw:
            continue
        link = entry.find("atom:link", NS)
        entries.append(
            {
                "video_id": video_id,
                "title": entry.findtext("atom:title", default="", namespaces=NS),
                "published_at": datetime.fromisoformat(published_raw.replace("Z", "+00:00")),
                "url": (link.get("href") if link is not None else None)
                or f"https://www.youtube.com/watch?v={video_id}",
                "description": entry.findtext("media:group/media:description", default="", namespaces=NS),
                "author": entry.findtext("atom:author/atom:name", default="", namespaces=NS),
            }
        )
    return entries


class TranscriptFetcher:
    """youtube-transcript-api のラッパー。

    YouTubeはクラウド事業者のIP（GitHub ActionsのAzure含む）を遮断しやすい。
    遮断・字幕なしの場合は None を返し、呼び出し側で概要欄にフォールバックする。
    """

    def __init__(self, proxy_url: str | None = None) -> None:
        self.proxy_url = proxy_url
        self.blocked = False
        self._api = None

    def _client(self):
        if self._api is None:
            from youtube_transcript_api import YouTubeTranscriptApi

            if self.proxy_url:
                from youtube_transcript_api.proxies import GenericProxyConfig

                self._api = YouTubeTranscriptApi(
                    proxy_config=GenericProxyConfig(http_url=self.proxy_url, https_url=self.proxy_url)
                )
            else:
                self._api = YouTubeTranscriptApi()
        return self._api

    def fetch(self, video_id: str) -> str | None:
        if self.blocked:
            return None
        from youtube_transcript_api import IpBlocked, RequestBlocked

        try:
            fetched = self._client().fetch(video_id, languages=TRANSCRIPT_LANGUAGES)
        except (RequestBlocked, IpBlocked):
            # 同じ実行内では以降も遮断されるので試行をやめる
            self.blocked = True
            return None
        except Exception:
            return None
        return " ".join(snippet.text for snippet in fetched if snippet.text).strip() or None


class YouTubeCollector:
    def __init__(
        self,
        *,
        max_videos_per_source: int = 3,
        initial_lookback_hours: int = 24,
        transcript_max_chars: int = 60000,
        transcript_fetcher: TranscriptFetcher | None = None,
        feed_fetcher: Callable[[str], bytes] = fetch_feed_xml,
    ) -> None:
        self.max_videos = max(1, max_videos_per_source)
        self.initial_lookback = timedelta(hours=initial_lookback_hours)
        self.transcript_max_chars = transcript_max_chars
        self.transcripts = transcript_fetcher or TranscriptFetcher()
        self._feed = feed_fetcher
        self.transcript_hits = 0
        self.transcript_misses = 0

    def fetch(self, source: Source, *, now: datetime) -> tuple[list[ContentItem], str | None]:
        entries = parse_feed(self._feed(source.handle))
        if source.cursor:
            since = datetime.fromisoformat(source.cursor)
        else:
            since = now - self.initial_lookback
        fresh = sorted(
            (e for e in entries if e["published_at"] > since),
            key=lambda e: e["published_at"],
        )
        if not fresh:
            return [], None
        # 上限を超える分は古い方から捨てる（最新を優先）
        selected = fresh[-self.max_videos :]
        items = [self._to_item(source, entry) for entry in selected]
        newest = max(e["published_at"] for e in fresh)
        return items, newest.isoformat()

    def _to_item(self, source: Source, entry: dict[str, Any]) -> ContentItem:
        transcript = self.transcripts.fetch(entry["video_id"])
        if transcript:
            self.transcript_hits += 1
            text = transcript[: self.transcript_max_chars]
            origin = "字幕"
        else:
            self.transcript_misses += 1
            text = entry["description"] or ""
            origin = "概要欄のみ"
        return ContentItem(
            platform=PLATFORM_YOUTUBE,
            item_id=entry["video_id"],
            author=source.name or entry["author"],
            url=entry["url"],
            published_at=entry["published_at"],
            text=text,
            text_origin=origin,
            title=entry["title"],
        )
