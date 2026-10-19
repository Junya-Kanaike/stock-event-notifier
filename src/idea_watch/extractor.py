from __future__ import annotations

import json
from typing import Any, Callable

from src.idea_watch.http import request_json
from src.idea_watch.models import (
    DIRECTIONS,
    EVIDENCE_LEVELS,
    HORIZONS,
    MARKETS,
    ContentItem,
    Idea,
    PLATFORM_X,
)


ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TOOL_NAME = "record_ideas"
X_BATCH_SIZE = 20

SYSTEM_PROMPT = """あなたは日本株中心の個人投資家のリサーチアシスタントです。
SNS投稿やYouTube動画の字幕・概要欄から「個別銘柄の投資アイデア」だけを抽出します。

抽出する条件:
- 特定の銘柄（企業名・証券コード・ティッカー）に対する見方・売買の考え・注目理由が述べられている
- 市場全体の感想、指数の上下だけの話、日記、宣伝、プレゼント企画、有料サロン勧誘は抽出しない
- 同じ投稿・動画で複数銘柄が語られていれば銘柄ごとに分ける
- 「この銘柄は注意」「売られすぎ」など弱い言及でも、理由があれば方向=ウォッチで残す
- 言及があっても理由が一切ない銘柄名の羅列は抽出しない

書き方:
- 発信者の主張を要約する。あなた自身の売買推奨や評価は加えない
- 原文を長く引用しない。自分の言葉で短くまとめる
- 日本株は4桁（または英字入り）の証券コード、米国株はティッカーを銘柄コードに入れる。
  確信が持てないコードは推測せず空文字にし、銘柄名だけ入れる
- 根拠の具体性: 数字・決算・開示・受注など検証可能な事実に基づけば「高」、
  業界動向や需給の一般論なら「中」、雰囲気・チャートの印象・煽りだけなら「低」
- 該当がなければ ideas を空配列にする
"""


def _tool_schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "抽出した投資アイデアを記録する。該当なしなら空配列。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ideas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_index": {
                                "type": "integer",
                                "description": "入力の [n] 番号",
                            },
                            "headline": {"type": "string", "description": "40字以内の見出し"},
                            "ticker": {"type": "string"},
                            "company": {"type": "string"},
                            "market": {"type": "string", "enum": list(MARKETS)},
                            "direction": {"type": "string", "enum": list(DIRECTIONS)},
                            "themes": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "AI半導体、インバウンドなど短いテーマ語。最大3つ",
                            },
                            "thesis": {"type": "string", "description": "主張の根拠。200字以内"},
                            "catalyst": {
                                "type": "string",
                                "description": "決算・イベントなど具体的なきっかけ。なければ空文字",
                            },
                            "horizon": {"type": "string", "enum": list(HORIZONS)},
                            "evidence": {"type": "string", "enum": list(EVIDENCE_LEVELS)},
                            "summary": {
                                "type": "string",
                                "description": "その銘柄についての発信内容の要約。400字以内",
                            },
                        },
                        "required": [
                            "source_index",
                            "headline",
                            "ticker",
                            "company",
                            "market",
                            "direction",
                            "themes",
                            "thesis",
                            "catalyst",
                            "horizon",
                            "evidence",
                            "summary",
                        ],
                    },
                }
            },
            "required": ["ideas"],
        },
    }


def format_items(items: list[ContentItem]) -> str:
    blocks = []
    for idx, item in enumerate(items):
        header = f"[{idx}] {item.platform} / {item.author} / {item.published_at.isoformat()} / 本文の種類: {item.text_origin}"
        if item.title:
            header += f"\nタイトル: {item.title}"
        blocks.append(f"{header}\n{item.text}")
    return "\n\n---\n\n".join(blocks)


class IdeaExtractor:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        holdings: list[tuple[str, str]] | None = None,
        requester: Callable[..., dict[str, Any]] = request_json,
    ) -> None:
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        self.model = model
        self.holdings = holdings or []
        self._request = requester
        self.input_tokens = 0
        self.output_tokens = 0

    def extract(self, items: list[ContentItem]) -> list[Idea]:
        ideas: list[Idea] = []
        for batch in batched(items):
            if not any(item.text.strip() or item.title for item in batch):
                continue
            ideas.extend(self._extract_batch(batch))
        return ideas

    def _extract_batch(self, batch: list[ContentItem]) -> list[Idea]:
        body = {
            "model": self.model,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "tools": [_tool_schema()],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [
                {
                    "role": "user",
                    "content": "次の発信から投資アイデアを抽出してください。\n\n" + format_items(batch),
                }
            ],
        }
        data = self._request(
            "POST",
            ANTHROPIC_API,
            service="Anthropic",
            headers=self._headers,
            json_body=body,
            timeout=120,
        )
        usage = data.get("usage") or {}
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        raw_ideas = parse_tool_output(data)
        return [idea for raw in raw_ideas if (idea := self._to_idea(raw, batch)) is not None]

    def _to_idea(self, raw: dict[str, Any], batch: list[ContentItem]) -> Idea | None:
        try:
            item = batch[int(raw.get("source_index", -1))]
        except (ValueError, IndexError):
            return None
        ticker = str(raw.get("ticker") or "").strip().upper()
        company = str(raw.get("company") or "").strip()
        if not ticker and not company:
            return None
        idea = Idea(
            item=item,
            headline=str(raw.get("headline") or company or ticker).strip(),
            ticker=ticker,
            company=company,
            market=_enum(raw.get("market"), MARKETS, "その他"),
            direction=_enum(raw.get("direction"), DIRECTIONS, "ウォッチ"),
            themes=[str(t).strip() for t in (raw.get("themes") or []) if str(t).strip()][:3],
            thesis=str(raw.get("thesis") or "").strip(),
            catalyst=str(raw.get("catalyst") or "").strip(),
            horizon=_enum(raw.get("horizon"), HORIZONS, "中期"),
            evidence=_enum(raw.get("evidence"), EVIDENCE_LEVELS, "低"),
            summary=str(raw.get("summary") or "").strip(),
        )
        idea.holding_related = is_holding(idea, self.holdings)
        return idea


def batched(items: list[ContentItem]) -> list[list[ContentItem]]:
    """X投稿は同一発信者ごとに最大20件まとめ、動画は1本ずつ処理する。"""
    batches: list[list[ContentItem]] = []
    x_by_author: dict[str, list[ContentItem]] = {}
    for item in items:
        if item.platform == PLATFORM_X:
            x_by_author.setdefault(item.author, []).append(item)
        else:
            batches.append([item])
    for posts in x_by_author.values():
        for i in range(0, len(posts), X_BATCH_SIZE):
            batches.append(posts[i : i + X_BATCH_SIZE])
    return batches


def parse_tool_output(data: dict[str, Any]) -> list[dict[str, Any]]:
    for block in data.get("content") or []:
        if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
            payload = block.get("input") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            ideas = payload.get("ideas") or []
            return [i for i in ideas if isinstance(i, dict)]
    return []


def is_holding(idea: Idea, holdings: list[tuple[str, str]]) -> bool:
    for code, name in holdings:
        if code and idea.ticker and code == idea.ticker:
            return True
        if name and idea.company and (name in idea.company or idea.company in name):
            return True
    return False


def _enum(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default
