from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


PLATFORM_X = "X"
PLATFORM_YOUTUBE = "YouTube"

DIRECTIONS = ("ロング", "ショート", "ウォッチ")
MARKETS = ("日本株", "米国株", "その他")
HORIZONS = ("短期", "中期", "長期")
EVIDENCE_LEVELS = ("高", "中", "低")


@dataclass
class Source:
    """Notion「監視ソース」の1行。"""

    page_id: str
    name: str
    platform: str
    handle: str
    internal_id: str | None = None
    cursor: str | None = None


@dataclass
class ContentItem:
    """1件の投稿または動画。"""

    platform: str
    item_id: str
    author: str
    url: str
    published_at: datetime
    text: str
    text_origin: str  # "投稿" / "字幕" / "概要欄のみ"
    title: str | None = None


@dataclass
class Idea:
    item: ContentItem
    headline: str
    ticker: str
    company: str
    market: str
    direction: str
    themes: list[str]
    thesis: str
    catalyst: str
    horizon: str
    evidence: str
    summary: str
    holding_related: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def source_key(self) -> str:
        prefix = "x" if self.item.platform == PLATFORM_X else "yt"
        ticker = self.ticker or self.company or "unknown"
        return f"{prefix}:{self.item.item_id}:{ticker}"
