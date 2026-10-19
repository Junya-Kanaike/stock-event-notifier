from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


PLATFORM_X = "X"
PLATFORM_X_SEARCH = "X検索"
PLATFORM_YOUTUBE = "YouTube"
X_PLATFORMS = (PLATFORM_X, PLATFORM_X_SEARCH)

STRATEGY_TYPES = (
    "需給イベント",
    "指数・リバランス",
    "ペア・相対価値",
    "市場構造・アノマリー",
    "定量シグナル",
    "テーマ資金循環",
)
DIRECTIONS = ("ロング", "ショート", "ロングショート", "ウォッチ")
HORIZONS = ("日計り", "数日", "数週間", "数ヶ月")
REPRODUCIBILITY = ("高", "中", "低")


@dataclass
class Source:
    """Notion「監視ソース」の1行。"""

    page_id: str
    name: str
    platform: str
    handle: str  # Xはユーザー名、X検索はクエリ、YouTubeはチャンネルID
    internal_id: str | None = None
    cursor: str | None = None


@dataclass
class ContentItem:
    """1件の投稿または動画。"""

    platform: str  # X / YouTube（X検索で取れた投稿も X）
    item_id: str
    author: str
    url: str
    published_at: datetime
    text: str
    text_origin: str  # "投稿" / "動画(Gemini)" / "字幕" / "概要欄のみ"
    title: str | None = None


@dataclass
class Idea:
    """イベントドリブン型の戦略アイデア（1投稿から複数出ることもある）。"""

    item: ContentItem
    headline: str
    strategy_type: str
    direction: str
    long_leg: str
    short_leg: str
    tickers: list[str]
    trigger: str
    event_date: date | None
    entry: str
    exit: str
    mechanism: str
    validation: str
    reproducibility: str
    horizon: str
    caveats: str
    summary: str
    holding_related: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def source_key(self) -> str:
        prefix = "x" if self.item.platform == PLATFORM_X else "yt"
        basis = ",".join(sorted(self.tickers)) or self.headline
        digest = hashlib.sha1(f"{self.strategy_type}|{basis}".encode("utf-8")).hexdigest()[:10]
        return f"{prefix}:{self.item.item_id}:{digest}"
