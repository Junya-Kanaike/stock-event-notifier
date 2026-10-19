from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

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
    inbox_processed: int = 0
    inbox_failed: int = 0
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    printed: list[Idea] = field(default_factory=list)

    def summary_line(self) -> str:
        # publicリポジトリのActionsログに出るため、件数だけを出す
        return (
            f"sources={self.sources} items={self.items} ideas={self.ideas_found} "
            f"written={self.ideas_written} duplicates={self.duplicates} "
            f"inbox={self.inbox_processed} inbox_failed={self.inbox_failed} "
            f"failures={len(self.failures)} skipped={len(self.skipped)}"
        )


def _is_quota_error(exc: Exception) -> bool:
    return type(exc).__name__ == "QuotaExhausted"


def _out_of_budget(extractor: Any) -> bool:
    remaining = getattr(extractor, "remaining_requests", None)
    return remaining is not None and remaining <= 0


def fit_to_budget(items: list[ContentItem], extractor: Any) -> list[ContentItem]:
    """抽出器の残りリクエスト数で処理できる分だけ、古い順に残す。"""
    remaining = getattr(extractor, "remaining_requests", None)
    if remaining is None:
        return items
    ordered = sorted(items, key=lambda i: i.published_at)
    for k in range(len(ordered), -1, -1):
        if extractor.requests_needed(ordered[:k]) <= remaining:
            return ordered[:k]
    return []  # pragma: no cover


def cursor_for(item: ContentItem) -> str:
    return item.published_at.isoformat() if item.platform == PLATFORM_YOUTUBE else item.item_id


def run_pipeline(
    *,
    store,
    extractor,
    x_collector,
    youtube_collector: Collector,
    now: datetime,
    dry_run: bool = False,
    inbox_builder=None,
    inbox_data_source_id: str | None = None,
) -> RunReport:
    report = RunReport()
    # 受信箱（自分で選んだ投稿）を先に処理する。無料枠を優先的に使う。
    if inbox_builder is not None and inbox_data_source_id:
        _process_inbox(store, extractor, inbox_builder, inbox_data_source_id, report, dry_run)

    sources = store.list_sources()
    report.sources = len(sources)
    for index, source in enumerate(sources):
        label = f"{source.platform}#{index + 1}"  # 名前はログに出さない
        try:
            if _out_of_budget(extractor):
                report.skipped.append(f"{label}: 抽出の無料枠上限（次回に回す）")
                continue
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
            kept = fit_to_budget(items, extractor)
            if items and not kept:
                report.skipped.append(f"{label}: 抽出の無料枠上限（次回に回す）")
                continue
            if len(kept) < len(items):
                # 処理できた最後の1件までカーソルを進め、残りは次回
                next_cursor = cursor_for(kept[-1])
                report.skipped.append(f"{label}: {len(items) - len(kept)}件を次回に回す")
            report.items += len(kept)
            ideas = extractor.extract(kept) if kept else []
            report.ideas_found += len(ideas)
            _write_ideas(store, ideas, report, dry_run)
            if not dry_run:
                store.update_source(source, cursor=next_cursor, fetched_at=now)
        except Exception as exc:  # 1ソースの失敗で他を止めない
            if _is_quota_error(exc):
                report.skipped.append(f"{label}: 抽出の無料枠上限（次回に回す）")
            else:
                report.failures.append(f"{label}: {describe_error(exc)}")
    return report


def _write_ideas(store, ideas: list[Idea], report: RunReport, dry_run: bool) -> None:
    for idea in ideas:
        if dry_run:
            report.printed.append(idea)
            continue
        if store.idea_exists(idea.source_key):
            report.duplicates += 1
            continue
        store.create_idea(idea)
        report.ideas_written += 1


def _process_inbox(store, extractor, builder, inbox_ds: str, report: RunReport, dry_run: bool) -> None:
    try:
        entries = store.list_inbox(inbox_ds)
    except Exception as exc:
        report.failures.append(f"受信箱: {describe_error(exc)}")
        return
    built: list[tuple[Any, ContentItem]] = []
    for entry in entries:
        try:
            built.append((entry, builder.build(entry)))
        except Exception as exc:
            report.inbox_failed += 1
            if not dry_run:
                store.mark_inbox(entry.page_id, "取得失敗", f"本文を取得できませんでした（{describe_error(exc)}）")
    if not built:
        return
    kept_items = fit_to_budget([item for _, item in built], extractor)
    kept_ids = {id(item) for item in kept_items}
    kept = [(entry, item) for entry, item in built if id(item) in kept_ids]
    if len(kept) < len(built):
        report.skipped.append(f"受信箱: {len(built) - len(kept)}件を次回に回す")
    if not kept:
        return
    try:
        ideas = extractor.extract([item for _, item in kept])
    except Exception as exc:
        if _is_quota_error(exc):
            report.skipped.append("受信箱: 抽出の無料枠上限（次回に回す）")
        else:
            report.failures.append(f"受信箱: {describe_error(exc)}")
        return
    report.ideas_found += len(ideas)
    _write_ideas(store, ideas, report, dry_run)
    for entry, item in kept:
        count = sum(1 for idea in ideas if idea.item is item)
        report.inbox_processed += 1
        if not dry_run:
            result = f"アイデア{count}件を追加" if count else "該当なし（やり方に合う戦略は見つからず）"
            store.mark_inbox(entry.page_id, "処理済み", result)


def describe_error(exc: Exception) -> str:
    """URL（チャンネルIDなど）を含みうる例外メッセージはログに出さない。"""
    from src.idea_watch.http import ApiError

    if isinstance(exc, ApiError):
        return str(exc)
    message = str(exc)
    if type(exc).__name__ == "InboxFetchError" and "http" not in message:
        return message
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")
