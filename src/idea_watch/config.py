from __future__ import annotations

import os
from dataclasses import dataclass, field


DEFAULT_MODEL = "claude-sonnet-5-5"


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def parse_holdings(raw: str | None) -> list[tuple[str, str]]:
    """"285A:キオクシア,6857:アドバンテスト" を [(code, name), ...] にする。

    コードだけ・名前だけの指定も許容する。
    """
    holdings: list[tuple[str, str]] = []
    for chunk in (raw or "").replace("、", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            code, name = chunk.split(":", 1)
        elif chunk[:4].isalnum() and len(chunk) <= 5 and any(c.isdigit() for c in chunk):
            code, name = chunk, ""
        else:
            code, name = "", chunk
        holdings.append((code.strip().upper(), name.strip()))
    return holdings


@dataclass
class Settings:
    notion_token: str
    sources_data_source_id: str
    ideas_data_source_id: str
    anthropic_api_key: str
    x_bearer_token: str | None
    model: str = DEFAULT_MODEL
    # 初回（カーソル未設定）に遡る時間
    initial_lookback_hours: int = 24
    # Xのコスト上限（1投稿読み取りごとに課金されるため）
    x_max_posts_per_source: int = 20
    x_max_posts_per_run: int = 300
    x_include_replies: bool = False
    youtube_max_videos_per_source: int = 3
    transcript_max_chars: int = 60000
    youtube_proxy_url: str | None = None
    holdings: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [
            name
            for name in [
                "NOTION_TOKEN",
                "IDEA_SOURCES_DATA_SOURCE_ID",
                "IDEA_IDEAS_DATA_SOURCE_ID",
                "ANTHROPIC_API_KEY",
            ]
            if not os.getenv(name, "").strip()
        ]
        if missing:
            raise RuntimeError("Missing required settings: " + ", ".join(missing))
        return cls(
            notion_token=os.environ["NOTION_TOKEN"].strip(),
            sources_data_source_id=os.environ["IDEA_SOURCES_DATA_SOURCE_ID"].strip(),
            ideas_data_source_id=os.environ["IDEA_IDEAS_DATA_SOURCE_ID"].strip(),
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"].strip(),
            x_bearer_token=os.getenv("X_BEARER_TOKEN", "").strip() or None,
            model=os.getenv("IDEA_MODEL", "").strip() or DEFAULT_MODEL,
            initial_lookback_hours=_int_env("IDEA_INITIAL_LOOKBACK_HOURS", 24),
            x_max_posts_per_source=_int_env("IDEA_X_MAX_POSTS_PER_SOURCE", 20),
            x_max_posts_per_run=_int_env("IDEA_X_MAX_POSTS_PER_RUN", 300),
            x_include_replies=os.getenv("IDEA_X_INCLUDE_REPLIES") == "1",
            youtube_max_videos_per_source=_int_env("IDEA_YT_MAX_VIDEOS_PER_SOURCE", 3),
            transcript_max_chars=_int_env("IDEA_TRANSCRIPT_MAX_CHARS", 60000),
            youtube_proxy_url=os.getenv("IDEA_YT_PROXY_URL", "").strip() or None,
            holdings=parse_holdings(os.getenv("IDEA_HOLDINGS")),
        )
