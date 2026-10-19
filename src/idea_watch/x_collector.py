from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from src.idea_watch.http import request_json
from src.idea_watch.models import ContentItem, PLATFORM_X, Source


X_API = "https://api.x.com/2"


class XCollector:
    """X API v2（従量課金）で指定アカウントの新着投稿を取る。

    課金は返ってきた投稿1件ごと。since_id で前回以降だけを読み、
    アカウント単位・実行単位の上限で読み取り件数を抑える。
    """

    def __init__(
        self,
        bearer_token: str,
        *,
        max_posts_per_source: int = 20,
        max_posts_per_run: int = 300,
        include_replies: bool = False,
        initial_lookback_hours: int = 24,
        requester: Callable[..., dict[str, Any]] = request_json,
    ) -> None:
        self._headers = {"Authorization": f"Bearer {bearer_token}"}
        self.max_posts_per_source = max(5, max_posts_per_source)
        self.remaining_budget = max(0, max_posts_per_run)
        self.include_replies = include_replies
        self.initial_lookback = timedelta(hours=initial_lookback_hours)
        self._request = requester
        self.posts_read = 0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", f"{X_API}{path}", service="X", headers=self._headers, params=params)

    def resolve_user_id(self, username: str) -> str:
        data = self._get(f"/users/by/username/{username}")
        user_id = (data.get("data") or {}).get("id")
        if not user_id:
            raise RuntimeError("X user not found")
        return str(user_id)

    def fetch(self, source: Source, *, now: datetime) -> tuple[list[ContentItem], str | None]:
        """新着投稿と、次回用のカーソル（最新のpost ID）を返す。"""
        if self.remaining_budget <= 0:
            return [], None
        user_id = source.internal_id
        if not user_id:
            raise RuntimeError("internal_id is required before fetching posts")

        limit = min(self.max_posts_per_source, self.remaining_budget)
        exclude = ["retweets"] if self.include_replies else ["retweets", "replies"]
        params: dict[str, Any] = {
            "max_results": max(5, min(100, limit)),
            "exclude": ",".join(exclude),
            "tweet.fields": "created_at,note_tweet,conversation_id",
        }
        if source.cursor:
            params["since_id"] = source.cursor
        else:
            start = (now - self.initial_lookback).astimezone(timezone.utc)
            params["start_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")

        posts: list[dict[str, Any]] = []
        newest_id: str | None = None
        while len(posts) < limit:
            data = self._get(f"/users/{user_id}/tweets", params)
            batch = data.get("data") or []
            meta = data.get("meta") or {}
            self.posts_read += len(batch)
            self.remaining_budget -= len(batch)
            if newest_id is None and meta.get("newest_id"):
                newest_id = str(meta["newest_id"])
            posts.extend(batch)
            next_token = meta.get("next_token")
            if not next_token or not batch or self.remaining_budget <= 0:
                break
            params["pagination_token"] = next_token
            params["max_results"] = max(5, min(100, limit - len(posts)))

        items = [self._to_item(source, post) for post in posts[:limit]]
        if newest_id is None and items:
            newest_id = max(items, key=lambda it: int(it.item_id)).item_id
        return items, newest_id

    @staticmethod
    def _to_item(source: Source, post: dict[str, Any]) -> ContentItem:
        text = ((post.get("note_tweet") or {}).get("text")) or post.get("text") or ""
        created = post.get("created_at")
        published = (
            datetime.fromisoformat(created.replace("Z", "+00:00")) if created else datetime.now(timezone.utc)
        )
        post_id = str(post["id"])
        return ContentItem(
            platform=PLATFORM_X,
            item_id=post_id,
            author=f"{source.name} (@{source.handle})",
            url=f"https://x.com/{source.handle}/status/{post_id}",
            published_at=published,
            text=text,
            text_origin="投稿",
        )
