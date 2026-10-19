from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from src.idea_watch.models import ContentItem, Idea, PLATFORM_X, PLATFORM_X_SEARCH, PLATFORM_YOUTUBE, Source


class Collector(Protocol):
    def fetch(self, source: Source, *, now: datetime) -> tuple[list[ContentItem], str | None]: ...


@dataclass
class RunReport:
    sources: int = 0
    items: int = 0
    ideas_found: int = 0
    ideas_written: int = 0
    duplicates: int = 0
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    printed: list[Idea] = field(default_factory=list)

    def summary_line(self) -> str:
        # publicリポジトリのActionsログに出るため、件数だけを出す
        return (
            f"sources={self.sources} items={self.items} ideas={self.ideas_found} "
            f"written={self.ideas_written} duplicates={self.duplicates} "
            f"failures={len(self.failures)} skipped={len(self.skipped)}"
        )


def run_pipeline(
    *,
    store,
    extractor,
    x_collector,
    youtube_collector: Collector,
    now: datetime,
    dry_run: bool = False,
) -> RunReport:
    report = RunReport()
    sources = store.list_sources()
    report.sources = len(sources)

    for index, source in enumerate(sources):
        label = f"{source.platform}#{index + 1}"  # 名前はログに出さない
        try:
            if source.platform in (PLATFORM_X, PLATFORM_X_SEARCH) and x_collector is None:
                report.skipped.append(f"{label}: X_BEARER_TOKEN未設定")
                continue
            if source.platform == PLATFORM_X:
                if not source.internal_id:
                    user_id = x_collector.resolve_user_id(source.handle)
                    if dry_run:
                        source.internal_id = user_id
                    else:
                        store.update_source(source, internal_id=user_id)
                fetch = x_collector.fetch
            elif source.platform == PLATFORM_X_SEARCH:
                fetch = x_collector.fetch_search
            elif source.platform == PLATFORM_YOUTUBE:
                fetch = youtube_collector.fetch
            else:  # pragma: no cover - parse_sourceで弾いている
                continue

            items, next_cursor = fetch(source, now=now)
            report.items += len(items)
            ideas = extractor.extract(items) if items else []
            report.ideas_found += len(ideas)

            for idea in ideas:
                if dry_run:
                    report.printed.append(idea)
                    continue
                if store.idea_exists(idea.source_key):
                    report.duplicates += 1
                    continue
                store.create_idea(idea)
                report.ideas_written += 1

            if not dry_run:
                store.update_source(source, cursor=next_cursor, fetched_at=now)
        except Exception as exc:  # 1ソースの失敗で他を止めない
            report.failures.append(f"{label}: {describe_error(exc)}")
    return report


def describe_error(exc: Exception) -> str:
    """URL（チャンネルIDなど）を含みうる例外メッセージはログに出さない。"""
    from src.idea_watch.http import ApiError

    if isinstance(exc, ApiError):
        return str(exc)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")
