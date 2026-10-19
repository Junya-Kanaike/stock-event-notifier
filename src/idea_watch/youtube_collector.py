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


GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_PROMPT = """この株式投資関連の動画を視聴し、日本語で書き起こし風に詳しく要約してください。
後で別のAIが「需給イベント・指数リバランス・ペアトレード・市場の癖・指標シグナル」型の
売買アイデアを抽出するための材料にします。次の情報は省略せず、話された順に書いてください。
- 言及された銘柄名・証券コード・ティッカー・指数
- 言及されたイベントと日付・時刻（増資、売出し、分売、TOB、指数入替・リバランス、分割、優待、
  ロックアップ、SQ、決算発表日など）
- 売買のタイミング（寄り付き・大引け・何日後など）、ロングとショートの組み合わせ
- なぜ株価が動くと考えているか（需給・裁定・参加者の行動）
- 過去事例・勝率・平均リターンなど数字
業績予想や企業評価の話も、出てきたなら短く記録してください（取捨選択は後段で行います）。
画面に映った表やチャートの数字も読み取れる範囲で書いてください。"""


class GeminiVideoDigester:
    """Gemini API に YouTube URL を渡して動画そのもの（音声＋映像）を読ませる。

    字幕取得と違い、YouTube側のIP遮断の影響を受けない（取得はGoogle側で行われる）。
    公開動画のみ対応。2026年10月時点でYouTube URL入力はプレビュー機能。
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        *,
        requester: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        from src.idea_watch.http import request_json

        self._headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        self.model = model
        self._request = requester or request_json
        self.input_tokens = 0
        self.failures = 0

    def digest(self, video_url: str) -> str | None:
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": GEMINI_PROMPT},
                        {"file_data": {"file_uri": video_url}},
                    ]
                }
            ],
            "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.2},
        }
        try:
            data = self._request(
                "POST",
                GEMINI_API.format(model=self.model),
                service="Gemini",
                headers=self._headers,
                json_body=body,
                timeout=300,
                retries=1,
            )
        except Exception:
            self.failures += 1
            return None
        usage = data.get("usageMetadata") or {}
        self.input_tokens += int(usage.get("promptTokenCount") or 0)
        texts = [
            part.get("text", "")
            for cand in (data.get("candidates") or [])[:1]
            for part in ((cand.get("content") or {}).get("parts") or [])
        ]
        text = "\n".join(t for t in texts if t).strip()
        if not text:
            self.failures += 1
        return text or None


class YouTubeCollector:
    def __init__(
        self,
        *,
        max_videos_per_source: int = 3,
        initial_lookback_hours: int = 24,
        transcript_max_chars: int = 60000,
        transcript_fetcher: TranscriptFetcher | None = None,
        gemini: GeminiVideoDigester | None = None,
        feed_fetcher: Callable[[str], bytes] = fetch_feed_xml,
    ) -> None:
        self.max_videos = max(1, max_videos_per_source)
        self.initial_lookback = timedelta(hours=initial_lookback_hours)
        self.transcript_max_chars = transcript_max_chars
        self.transcripts = transcript_fetcher or TranscriptFetcher()
        self.gemini = gemini
        self._feed = feed_fetcher
        self.gemini_hits = 0
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
        # 優先順: Gemini（動画を直接視聴）→ 字幕 → 概要欄
        digest = self.gemini.digest(f"https://www.youtube.com/watch?v={entry['video_id']}") if self.gemini else None
        if digest:
            self.gemini_hits += 1
            text, origin = digest[: self.transcript_max_chars], "動画(Gemini)"
            return self._make_item(source, entry, text, origin)
        transcript = self.transcripts.fetch(entry["video_id"])
        if transcript:
            self.transcript_hits += 1
            text = transcript[: self.transcript_max_chars]
            origin = "字幕"
        else:
            self.transcript_misses += 1
            text = entry["description"] or ""
            origin = "概要欄のみ"
        return self._make_item(source, entry, text, origin)

    @staticmethod
    def _make_item(source: Source, entry: dict[str, Any], text: str, origin: str) -> ContentItem:
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
