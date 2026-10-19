"""Notion「アイデア受信箱」に放り込まれたURL・メモを ContentItem にする。

X投稿は公式の oEmbed エンドポイント（publish.x.com/oembed、認証不要・無料）で本文を取る。
これは埋め込み表示用に公開されている仕組みで、スクレイピングではない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import requests

from src.idea_watch.models import ContentItem, PLATFORM_MEMO, PLATFORM_X, PLATFORM_YOUTUBE


OEMBED_URL = "https://publish.x.com/oembed"
URL_RE = re.compile(r"https?://[^\s<>\"'）)]+")
X_STATUS_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status(?:es)?/(\d+)")
YOUTUBE_RES = [
    re.compile(r"https?://(?:www\.|m\.)?youtube\.com/watch\?(?:[^#\s]*&)?v=([A-Za-z0-9_-]{11})"),
    re.compile(r"https?://youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"https?://(?:www\.|m\.)?youtube\.com/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})"),
]


@dataclass
class InboxEntry:
    page_id: str
    title: str
    url: str | None
    memo: str
    created_at: datetime


class InboxFetchError(RuntimeError):
    pass


def find_url(*texts: str) -> str | None:
    for text in texts:
        match = URL_RE.search(text or "")
        if match:
            return match.group(0)
    return None


def classify_url(url: str | None) -> tuple[str, str | None]:
    """(platform, id) を返す。X投稿なら status ID、YouTubeなら動画ID。"""
    if not url:
        return PLATFORM_MEMO, None
    match = X_STATUS_RE.match(url)
    if match:
        return PLATFORM_X, match.group(2)
    for pattern in YOUTUBE_RES:
        match = pattern.match(url)
        if match:
            return PLATFORM_YOUTUBE, match.group(1)
    return PLATFORM_MEMO, None


def fetch_x_oembed(url: str) -> dict[str, Any]:
    response = requests.get(
        OEMBED_URL,
        params={"url": url, "omit_script": "1", "dnt": "true", "lang": "ja", "hide_media": "1"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise InboxFetchError(f"oEmbed HTTP {response.status_code}")
    return response.json()


def parse_oembed_text(payload: dict[str, Any]) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(payload.get("html") or "", "html.parser")
    paragraphs = [p.get_text("\n", strip=True) for p in soup.find_all("p")]
    return "\n".join(t for t in paragraphs if t).strip()


class InboxItemBuilder:
    def __init__(
        self,
        *,
        youtube_item_factory: Callable[[str, InboxEntry], ContentItem],
        oembed_fetcher: Callable[[str], dict[str, Any]] = fetch_x_oembed,
    ) -> None:
        self._youtube = youtube_item_factory
        self._oembed = oembed_fetcher

    def build(self, entry: InboxEntry) -> ContentItem:
        url = entry.url or find_url(entry.title, entry.memo)
        platform, ext_id = classify_url(url)
        memo_note = f"\n\n保存者のメモ: {entry.memo}" if entry.memo else ""
        if platform == PLATFORM_X:
            payload = self._oembed(url)
            text = parse_oembed_text(payload)
            if not text:
                raise InboxFetchError("oEmbed returned no text")
            author = payload.get("author_name") or "X"
            return ContentItem(
                platform=PLATFORM_X,
                item_id=ext_id,
                author=f"{author}（受信箱）",
                url=f"https://x.com/i/web/status/{ext_id}",
                published_at=entry.created_at,
                text=text + memo_note,
                text_origin="受信箱",
            )
        if platform == PLATFORM_YOUTUBE:
            return self._youtube(ext_id, entry)
        body = "\n".join(t for t in [entry.title, entry.memo] if t).strip()
        if not body:
            raise InboxFetchError("empty inbox entry")
        return ContentItem(
            platform=PLATFORM_MEMO,
            item_id=entry.page_id.replace("-", ""),
            author="自分のメモ（受信箱）",
            url=url or "",
            published_at=entry.created_at,
            text=body,
            text_origin="受信箱",
        )
