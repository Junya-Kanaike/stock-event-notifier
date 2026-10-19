"""Gemini API（無料枠）で戦略アイデアを抽出する。

- 判定基準は Claude 版と同じ SYSTEM_PROMPT を使う
- YouTube 動画は URL をそのまま渡し、「視聴＋抽出」を1リクエストで行う
- 無料枠は1日・1分あたりのリクエスト数が少ないため、1回の実行で使う回数に上限を設け、
  リクエストの間隔を空ける。上限に達したら残りは次回に回す（カーソルを進めない）
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from src.idea_watch.extractor import SYSTEM_PROMPT, _tool_schema, format_items, raw_to_idea
from src.idea_watch.http import ApiError, request_json
from src.idea_watch.models import ContentItem, Idea


GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"
TEXT_BATCH_SIZE = 20
VIDEO_INSTRUCTION = (
    "添付のYouTube動画を最後まで視聴し（画面に映る表・数字も含めて）、"
    "条件に合う戦略アイデアだけを抽出してください。source_index は 0 です。\n\n"
)
JSON_INSTRUCTION = (
    "\n\n出力は JSON のみ。形式: {\"ideas\": [ ... ]}。各要素のキーは "
    "source_index, headline, strategy_type, direction, long_leg, short_leg, tickers, trigger, "
    "event_date, entry, exit, mechanism, validation, reproducibility, horizon, caveats, summary。"
)


class QuotaExhausted(RuntimeError):
    """無料枠のリクエスト上限（実行内の上限、またはAPIの429）に達した。"""


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema の小さなサブセットを Gemini の responseSchema 形式に変換する。"""
    out: dict[str, Any] = {"type": str(schema.get("type", "string")).upper()}
    if "description" in schema:
        out["description"] = schema["description"]
    if "enum" in schema:
        out["enum"] = list(schema["enum"])
    if "properties" in schema:
        out["properties"] = {k: to_gemini_schema(v) for k, v in schema["properties"].items()}
    if "required" in schema:
        out["required"] = list(schema["required"])
    if "items" in schema:
        out["items"] = to_gemini_schema(schema["items"])
    return out


RESPONSE_SCHEMA = to_gemini_schema(_tool_schema()["input_schema"])


class GeminiIdeaExtractor:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        holdings: list[tuple[str, str]] | None = None,
        max_requests: int = 9,
        min_interval_sec: float = 13.0,
        requester: Callable[..., dict[str, Any]] = request_json,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        self.model = model
        self.holdings = holdings or []
        self.remaining_requests: int | None = max(0, max_requests)
        self.min_interval = max(0.0, min_interval_sec)
        self._request = requester
        self._sleep = sleeper
        self._clock = clock
        self._last_call: float | None = None
        self.requests_made = 0
        self.input_tokens = 0
        self.output_tokens = 0

    # --- 予算 -----------------------------------------------------------------

    @staticmethod
    def batches(items: list[ContentItem]) -> list[list[ContentItem]]:
        """動画は1本ずつ、テキストは発信者に関係なく20件ずつまとめる（無料枠の回数節約）。"""
        videos = [[i] for i in items if i.video_url]
        texts = [i for i in items if not i.video_url and (i.text.strip() or i.title)]
        return videos + [texts[k : k + TEXT_BATCH_SIZE] for k in range(0, len(texts), TEXT_BATCH_SIZE)]

    @classmethod
    def requests_needed(cls, items: list[ContentItem]) -> int:
        return len(cls.batches(items))

    # --- 抽出 -----------------------------------------------------------------

    def extract(self, items: list[ContentItem]) -> list[Idea]:
        ideas: list[Idea] = []
        for batch in self.batches(items):
            ideas.extend(self._extract_batch(batch))
        return ideas

    def _extract_batch(self, batch: list[ContentItem]) -> list[Idea]:
        if self.remaining_requests is not None and self.remaining_requests <= 0:
            raise QuotaExhausted("per-run Gemini request budget exhausted")
        if len(batch) == 1 and batch[0].video_url:
            item = batch[0]
            context = format_items([item])
            parts: list[dict[str, Any]] = [
                {"text": VIDEO_INSTRUCTION + "動画の情報:\n" + context + JSON_INSTRUCTION},
                {"file_data": {"file_uri": item.video_url}},
            ]
        else:
            parts = [
                {
                    "text": "次の発信から、条件に合う戦略アイデアだけを抽出してください。\n\n"
                    + format_items(batch)
                    + JSON_INSTRUCTION
                }
            ]
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
            },
        }
        self._pace()
        try:
            data = self._request(
                "POST",
                GEMINI_API.format(model=self.model),
                service="Gemini",
                headers=self._headers,
                json_body=body,
                timeout=300,
                retries=1,
            )
        except ApiError as exc:
            if exc.status == 429:
                self.remaining_requests = 0
                raise QuotaExhausted("Gemini API quota (HTTP 429)") from exc
            raise
        finally:
            self.requests_made += 1
            if self.remaining_requests is not None:
                self.remaining_requests = max(0, self.remaining_requests - 1)
        usage = data.get("usageMetadata") or {}
        self.input_tokens += int(usage.get("promptTokenCount") or 0)
        self.output_tokens += int(usage.get("candidatesTokenCount") or 0)
        return [
            idea for raw in parse_gemini_ideas(data) if (idea := raw_to_idea(raw, batch, self.holdings)) is not None
        ]

    def _pace(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()


def parse_gemini_ideas(data: dict[str, Any]) -> list[dict[str, Any]]:
    texts = [
        part.get("text", "")
        for cand in (data.get("candidates") or [])[:1]
        for part in ((cand.get("content") or {}).get("parts") or [])
    ]
    text = "".join(texts).strip()
    if not text:
        return []
    payload: Any
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    ideas = payload.get("ideas") if isinstance(payload, dict) else payload
    return [i for i in (ideas or []) if isinstance(i, dict)]
