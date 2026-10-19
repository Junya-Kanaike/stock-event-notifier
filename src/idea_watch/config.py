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
    anthropic_api_key: str | None
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
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"
    # 抽出に使うLLM。"gemini"（無料枠）か "claude"（従量課金）
    llm: str = "gemini"
    # Gemini無料枠の保護: 1回の実行で使うリクエスト数と、リクエスト間隔（秒）
    gemini_max_requests: int = 9
    gemini_min_interval_sec: float = 13.0
    inbox_data_source_id: str | None = None
    holdings: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [
            name
            for name in [
                "NOTION_TOKEN",
                "IDEA_SOURCES_DATA_SOURCE_ID",
                "IDEA_IDEAS_DATA_SOURCE_ID",
            ]
            if not os.getenv(name, "").strip()
        ]
        anthropic = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
        gemini = os.getenv("GEMINI_API_KEY", "").strip() or None
        if not anthropic and not gemini:
            missing.append("GEMINI_API_KEY (or ANTHROPIC_API_KEY)")
        if missing:
            raise RuntimeError("Missing required settings: " + ", ".join(missing))
        llm = (os.getenv("IDEA_LLM", "").strip().lower() or ("gemini" if gemini else "claude"))
        if llm not in ("gemini", "claude"):
            raise ValueError("IDEA_LLM must be 'gemini' or 'claude'")
        if llm == "gemini" and not gemini:
            raise RuntimeError("IDEA_LLM=gemini requires GEMINI_API_KEY")
        if llm == "claude" and not anthropic:
            raise RuntimeError("IDEA_LLM=claude requires ANTHROPIC_API_KEY")
        return cls(
            notion_token=os.environ["NOTION_TOKEN"].strip(),
            sources_data_source_id=os.environ["IDEA_SOURCES_DATA_SOURCE_ID"].strip(),
            ideas_data_source_id=os.environ["IDEA_IDEAS_DATA_SOURCE_ID"].strip(),
            anthropic_api_key=anthropic,
            x_bearer_token=os.getenv("X_BEARER_TOKEN", "").strip() or None,
            model=os.getenv("IDEA_MODEL", "").strip() or DEFAULT_MODEL,
            initial_lookback_hours=_int_env("IDEA_INITIAL_LOOKBACK_HOURS", 24),
            x_max_posts_per_source=_int_env("IDEA_X_MAX_POSTS_PER_SOURCE", 20),
            x_max_posts_per_run=_int_env("IDEA_X_MAX_POSTS_PER_RUN", 300),
            x_include_replies=os.getenv("IDEA_X_INCLUDE_REPLIES") == "1",
            youtube_max_videos_per_source=_int_env("IDEA_YT_MAX_VIDEOS_PER_SOURCE", 3),
            transcript_max_chars=_int_env("IDEA_TRANSCRIPT_MAX_CHARS", 60000),
            youtube_proxy_url=os.getenv("IDEA_YT_PROXY_URL", "").strip() or None,
            gemini_api_key=gemini,
            llm=llm,
            gemini_max_requests=_int_env("IDEA_GEMINI_MAX_REQUESTS", 9),
            gemini_min_interval_sec=float(os.getenv("IDEA_GEMINI_MIN_INTERVAL_SEC", "").strip() or 13),
            inbox_data_source_id=os.getenv("IDEA_INBOX_DATA_SOURCE_ID", "").strip() or None,
            gemini_model=os.getenv("IDEA_GEMINI_MODEL", "").strip() or "gemini-3.8-flash",
            holdings=parse_holdings(os.getenv("IDEA_HOLDINGS")),
        )
