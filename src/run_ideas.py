"""YouTube / X の指定ソースとNotionの受信箱から投資アイデアを抽出し Notion に書き込む。

python -m src.run_ideas            # 本番（Notionへ書き込み・カーソル更新）
python -m src.run_ideas --dry-run  # 抽出結果を標準出力に表示（ローカル確認用。Actionsでは使わない）
"""
from __future__ import annotations

import argparse
import os
import sys

from src.core.bizday import now_jst
from src.idea_watch.config import Settings
from src.idea_watch.extractor import IdeaExtractor
from src.idea_watch.gemini_extractor import GeminiIdeaExtractor
from src.idea_watch.inbox import InboxEntry, InboxItemBuilder
from src.idea_watch.models import Source
from src.idea_watch.notion_store import NotionStore
from src.idea_watch.pipeline import RunReport, run_pipeline
from src.idea_watch.x_collector import XCollector
from src.idea_watch.youtube_collector import GeminiVideoDigester, TranscriptFetcher, YouTubeCollector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    now = now_jst()

    store = NotionStore(settings.notion_token, settings.sources_data_source_id, settings.ideas_data_source_id)
    use_gemini = settings.llm == "gemini"
    if use_gemini:
        extractor = GeminiIdeaExtractor(
            settings.gemini_api_key,
            settings.gemini_model,
            holdings=settings.holdings,
            max_requests=settings.gemini_max_requests,
            min_interval_sec=settings.gemini_min_interval_sec,
        )
    else:
        extractor = IdeaExtractor(settings.anthropic_api_key, settings.model, holdings=settings.holdings)
    x_collector = (
        XCollector(
            settings.x_bearer_token,
            max_posts_per_source=settings.x_max_posts_per_source,
            max_posts_per_run=settings.x_max_posts_per_run,
            include_replies=settings.x_include_replies,
            initial_lookback_hours=settings.initial_lookback_hours,
        )
        if settings.x_bearer_token
        else None
    )
    # Geminiで抽出する構成では、動画URLを抽出器に直接渡す（視聴＋抽出を1リクエスト）。
    # Claudeで抽出する構成では、Geminiキーがあれば動画の要約だけGeminiに任せる。
    gemini = (
        GeminiVideoDigester(settings.gemini_api_key, settings.gemini_model)
        if settings.gemini_api_key and not use_gemini
        else None
    )
    youtube_collector = YouTubeCollector(
        max_videos_per_source=settings.youtube_max_videos_per_source,
        initial_lookback_hours=settings.initial_lookback_hours,
        transcript_max_chars=settings.transcript_max_chars,
        transcript_fetcher=TranscriptFetcher(settings.youtube_proxy_url),
        gemini=gemini,
        defer_video_to_extractor=use_gemini,
    )

    def youtube_from_inbox(video_id: str, entry: InboxEntry):
        meta = {
            "video_id": video_id,
            "title": entry.title,
            "published_at": entry.created_at,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "description": entry.memo,
            "author": "YouTube（受信箱）",
        }
        item = youtube_collector._to_item(Source("inbox", "YouTube（受信箱）", "YouTube", video_id), meta)
        if entry.memo:
            item.text = (item.text + f"\n\n保存者のメモ: {entry.memo}").strip()
        return item

    inbox_builder = InboxItemBuilder(youtube_item_factory=youtube_from_inbox)

    report = run_pipeline(
        store=store,
        extractor=extractor,
        x_collector=x_collector,
        youtube_collector=youtube_collector,
        now=now,
        dry_run=args.dry_run,
        inbox_builder=inbox_builder if settings.inbox_data_source_id else None,
        inbox_data_source_id=settings.inbox_data_source_id,
    )

    print(report.summary_line())
    if x_collector is not None:
        print(f"x_posts_read={x_collector.posts_read} (est. ${x_collector.posts_read * 0.005:.2f})")
    if gemini is not None:
        print(f"youtube_gemini ok={youtube_collector.gemini_hits} failed={gemini.failures} tokens={gemini.input_tokens}")
    print(
        f"youtube_transcripts ok={youtube_collector.transcript_hits} "
        f"fallback={youtube_collector.transcript_misses} blocked={youtube_collector.transcripts.blocked}"
    )
    if use_gemini:
        print(
            f"gemini requests={extractor.requests_made} remaining={extractor.remaining_requests} "
            f"tokens in={extractor.input_tokens} out={extractor.output_tokens}"
        )
    else:
        print(f"claude_tokens in={extractor.input_tokens} out={extractor.output_tokens}")

    if args.dry_run:
        _print_ideas(report)
    else:
        _alert_if_needed(report)
    for line in report.failures:
        print(f"FAILED {line}", file=sys.stderr)
    # 全ソース失敗のときだけ非ゼロ終了（部分失敗はSlack通知で知らせる）
    return 1 if report.sources and len(report.failures) >= report.sources else 0


def _print_ideas(report: RunReport) -> None:
    for idea in report.printed:
        print("-" * 60)
        print(f"[{idea.strategy_type}] {idea.headline} | {idea.direction} / {idea.horizon} / 再現性{idea.reproducibility}")
        if idea.long_leg or idea.short_leg:
            print(f"  ロング: {idea.long_leg or '-'}  ショート: {idea.short_leg or '-'}")
        print(f"  トリガー: {idea.trigger}" + (f"（{idea.event_date}）" if idea.event_date else ""))
        print(f"  入: {idea.entry}  出: {idea.exit}")
        print(f"  なぜ: {idea.mechanism}")
        print(f"  検証: {idea.validation}")
        print(f"  {idea.item.url} ({idea.item.text_origin})")


def _alert_if_needed(report: RunReport) -> None:
    # 字幕遮断は毎回起こりうるので通知しない（Notionの「抽出元」とログで確認する）
    if not os.getenv("SLACK_WEBHOOK_SYSTEM") or not report.failures:
        return
    lines = [f"アイデア監視: {len(report.failures)}/{report.sources}ソースで取得失敗"]
    lines.extend(f"• {f}" for f in report.failures[:10])
    try:
        from src.notifiers.slack import SlackNotifier

        SlackNotifier().system("\n".join(lines))
    except Exception as exc:  # 通知失敗で本処理を失敗扱いにしない
        print(f"slack alert failed: {type(exc).__name__}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
