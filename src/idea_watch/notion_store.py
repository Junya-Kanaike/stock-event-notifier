from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from src.idea_watch.http import request_json
from src.idea_watch.models import Idea, PLATFORM_X, PLATFORM_YOUTUBE, Source


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
RICH_TEXT_LIMIT = 2000

# Notionの「監視ソース」プロパティ名
P_NAME = "名前"
P_PLATFORM = "プラットフォーム"
P_HANDLE = "ハンドル"
P_ENABLED = "有効"
P_INTERNAL_ID = "内部ID"
P_CURSOR = "カーソル"
P_LAST_FETCHED = "最終取得"


class NotionStore:
    def __init__(
        self,
        token: str,
        sources_data_source_id: str,
        ideas_data_source_id: str,
        *,
        requester: Callable[..., dict[str, Any]] = request_json,
    ) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.sources_ds = sources_data_source_id
        self.ideas_ds = ideas_data_source_id
        self._request = requester

    def _call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        return self._request(
            method,
            f"{NOTION_API}{path}",
            service="Notion",
            headers=self._headers,
            json_body=body,
        )

    # --- 監視ソース ---------------------------------------------------------

    def list_sources(self) -> list[Source]:
        body: dict[str, Any] = {
            "filter": {"property": P_ENABLED, "checkbox": {"equals": True}},
            "page_size": 100,
        }
        sources: list[Source] = []
        while True:
            data = self._call("POST", f"/data_sources/{self.sources_ds}/query", body)
            for page in data.get("results", []):
                source = parse_source(page)
                if source is not None:
                    sources.append(source)
            if not data.get("has_more"):
                break
            body["start_cursor"] = data["next_cursor"]
        return sources

    def update_source(
        self,
        source: Source,
        *,
        cursor: str | None = None,
        internal_id: str | None = None,
        fetched_at: datetime | None = None,
    ) -> None:
        props: dict[str, Any] = {}
        if cursor is not None:
            props[P_CURSOR] = rich_text(cursor)
            source.cursor = cursor
        if internal_id is not None:
            props[P_INTERNAL_ID] = rich_text(internal_id)
            source.internal_id = internal_id
        if fetched_at is not None:
            props[P_LAST_FETCHED] = {"date": {"start": fetched_at.isoformat()}}
        if props:
            self._call("PATCH", f"/pages/{source.page_id}", {"properties": props})

    # --- 投資アイデア -------------------------------------------------------

    def idea_exists(self, source_key: str) -> bool:
        body = {
            "filter": {"property": "ソースID", "rich_text": {"equals": source_key}},
            "page_size": 1,
        }
        data = self._call("POST", f"/data_sources/{self.ideas_ds}/query", body)
        return bool(data.get("results"))

    def create_idea(self, idea: Idea) -> None:
        self._call("POST", "/pages", build_idea_page(self.ideas_ds, idea))


def parse_source(page: dict[str, Any]) -> Source | None:
    props = page.get("properties", {})
    platform = ((props.get(P_PLATFORM) or {}).get("select") or {}).get("name")
    handle = plain_text(props.get(P_HANDLE)).strip().lstrip("@")
    if platform not in (PLATFORM_X, PLATFORM_YOUTUBE) or not handle:
        return None
    name = plain_text(props.get(P_NAME)).strip() or handle
    return Source(
        page_id=page["id"],
        name=name,
        platform=platform,
        handle=handle,
        internal_id=plain_text(props.get(P_INTERNAL_ID)).strip() or None,
        cursor=plain_text(props.get(P_CURSOR)).strip() or None,
    )


def plain_text(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    items = prop.get("title") if prop.get("type") == "title" or "title" in prop else prop.get("rich_text")
    return "".join(item.get("plain_text", "") for item in (items or []))


def rich_text(text: str) -> dict[str, Any]:
    return {"rich_text": _chunks(text)}


def _chunks(text: str) -> list[dict[str, Any]]:
    text = text or ""
    if not text:
        return []
    return [
        {"type": "text", "text": {"content": text[i : i + RICH_TEXT_LIMIT]}}
        for i in range(0, min(len(text), RICH_TEXT_LIMIT * 5), RICH_TEXT_LIMIT)
    ]


def _select(value: str | None) -> dict[str, Any]:
    return {"select": {"name": value}} if value else {"select": None}


def _paragraph(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _chunks(text)}}


def _heading(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _chunks(text)}}


def build_idea_page(ideas_data_source_id: str, idea: Idea) -> dict[str, Any]:
    item = idea.item
    published = item.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    themes = [{"name": t[:100].replace(",", "・")} for t in idea.themes[:5] if t.strip()]
    properties: dict[str, Any] = {
        "見出し": {"title": _chunks(idea.headline[:200])},
        "銘柄コード": rich_text(idea.ticker),
        "銘柄名": rich_text(idea.company),
        "市場": _select(idea.market),
        "方向": _select(idea.direction),
        "テーマ": {"multi_select": themes},
        "根拠": rich_text(idea.thesis),
        "カタリスト": rich_text(idea.catalyst),
        "時間軸": _select(idea.horizon),
        "根拠の具体性": _select(idea.evidence),
        "保有銘柄関連": {"checkbox": bool(idea.holding_related)},
        "発信者": rich_text(item.author),
        "プラットフォーム": _select(item.platform),
        "ソースURL": {"url": item.url},
        "投稿日時": {"date": {"start": published.isoformat()}},
        "ソースID": rich_text(idea.source_key),
        "ステータス": {"status": {"name": "未確認"}},
    }
    children = [
        _heading("要約"),
        _paragraph(idea.summary or idea.thesis),
        _heading("抽出元"),
        _paragraph(
            f"{item.platform} / {item.author} / 抽出元: {item.text_origin}"
            + (f"\n動画タイトル: {item.title}" if item.title else "")
        ),
        _paragraph("AIによる要約。発信者の主張の整理であり、売買判断の前に開示・決算などの一次情報で確認すること。"),
    ]
    return {
        "parent": {"type": "data_source_id", "data_source_id": ideas_data_source_id},
        "properties": properties,
        "children": children,
    }
