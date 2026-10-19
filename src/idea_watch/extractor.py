from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable

from src.idea_watch.http import request_json
from src.idea_watch.models import (
    DIRECTIONS,
    HORIZONS,
    REPRODUCIBILITY,
    STRATEGY_TYPES,
    ContentItem,
    Idea,
    PLATFORM_X,
)


ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TOOL_NAME = "record_ideas"
X_BATCH_SIZE = 20

# 利用者のNotionの戦略メモ（PO・分売・CB・第三者割当・指数除外・分割・TOB等の検証記録）
# から読み取った「戦略の作り方」を、そのままフィルタ条件にしている。
SYSTEM_PROMPT = """あなたは日本株のイベントドリブン投資家のリサーチアシスタントです。
SNS投稿やYouTube動画の内容から、この投資家のやり方に合う「戦略アイデア」だけを拾い、
それ以外はすべて捨てます。迷ったら捨ててください。通知の数より精度が大事です。

# この投資家の戦略の作り方
- 業績や企業価値を当てにいかない。「いつ・誰が・どれだけ・なぜ売買せざるを得ないか」という
  需給の歪みが、日付の決まったタイミングで生じる場面を狙う。
  例: PO（価格決定日・受渡日）、立会外分売、CB発行、第三者割当、自己株TOB、TOB不成立、
  指数の除外・組入れ・リバランス、TOPIXの段階的ウエイト低減、MSCI入替、株式分割の権利落ち、
  優待の新設・廃止、IPOのロックアップ解除、売り禁・増担保、配当再投資やETF分配金の売買、SQ。
- 連動するはずの銘柄・指数・海外関連株の値動きに一時的な乖離が出たときの、
  ロングショートでの差分取り（例: 寄り付き時点で日経平均・SKハイニックス・キオクシアの
  騰落率に歪みがある → 割安側ロング・割高側ショート）。
- 他の参加者の行動の癖を逆手に取る（例: リバランス狙いの空売り勢が15時過ぎに一斉に買い戻す）。
- 騰落レシオ・RSI・日経VIなど、指標の閾値で機械的に売買できるシグナル。
- テーマ物色でも、時期の決まったカタリストと大型株への資金流入がある場合に限る。
- 仮説（なぜ歪みが生じるか）→ 過去の同種事例で勝率・平均リターンを検証 → ルール化、の順で考える。
  エントリーとエグジットは「寄り付き」「大引け」「◯営業日後」のように時刻と日数で決め、
  値幅目標や気分では決めない。日中に張り付くデイトレードはしない。
- 地合い（日経VI 30以上で縮小、40以上で見送り）と、戦略の前提が今も成り立つかを常に気にする。

# 拾うもの（この3つがそろっていること）
1. 売買のきっかけが具体的なイベント・乖離・指標であり、日付や時刻が特定できるか、少なくとも
   「◯◯が発表されたら」という条件で書ける
2. なぜ株価が動くかが需給・裁定・参加者行動で説明できる
3. 過去の同種ケースを集めて検証できる（年に数回以上起きる、または過去事例が存在する）

# 捨てるもの
- 決算予想、受注残・業績の伸び、割安・割高、目標株価、ビジネスモデルや競争力の評価、
  長期の成長ストーリー（例:「関電工は次の決算で大型案件が乗って受注残が増える」）
- 決算プレー（決算を跨いで上か下かに賭ける）
- 理由のない銘柄推奨、煽り、ストップ高狙い、低位・小型・低浮動株の仕手的な話
- 市況の感想、指数の上下を語るだけの投稿、日記、宣伝、有料サロンの勧誘
- すでに終わったイベントの振り返りで、次に使えるルールが読み取れないもの
  （振り返りでも「次回も同じ型で取れる」と読み取れるなら拾ってよい）

# 書き方
- 発信者の主張を整理する。発信内容にない売買ルールを創作しない。エントリー・エグジットが
  語られていなければ、あなたの補足だと分かるように「（補足）」を付けて妥当な案を書く。
- 検証方法には、どのイベントを何件集め、どのタイミングで売買したリターンを比べるかを書く。
- 再現性: 同種イベントが年に何度も起き過去データで検証しやすいなら「高」、年に数回程度なら「中」、
  一度きりに近いなら「低」。
- 注意点には、地合い依存、空売り可否（貸借・非貸借、売り禁）、流動性、先回りされやすさなどを書く。
- 日本株は証券コード（4桁または英字入り）、海外株はティッカー（例: 000660.KS, MU）。
  不確かなコードは推測せず銘柄名だけを書く。
- 原文を長く引用しない。自分の言葉で短くまとめる。
- 該当がなければ ideas を空配列にする。"""


def _tool_schema() -> dict[str, Any]:
    text = {"type": "string"}
    return {
        "name": TOOL_NAME,
        "description": "この投資家のやり方に合う戦略アイデアを記録する。該当なしなら空配列。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ideas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_index": {"type": "integer", "description": "入力の [n] 番号"},
                            "headline": {"type": "string", "description": "40字以内の見出し"},
                            "strategy_type": {"type": "string", "enum": list(STRATEGY_TYPES)},
                            "direction": {"type": "string", "enum": list(DIRECTIONS)},
                            "long_leg": {"type": "string", "description": "買う側の銘柄（名前とコード）。なければ空"},
                            "short_leg": {"type": "string", "description": "売る側の銘柄・指数。なければ空"},
                            "tickers": {
                                "type": "array",
                                "items": text,
                                "description": "関係する銘柄コード・ティッカー（確かなものだけ）",
                            },
                            "trigger": {"type": "string", "description": "売買のきっかけとなるイベント・乖離・指標"},
                            "event_date": {
                                "type": "string",
                                "description": "イベントの実施日・基準日 YYYY-MM-DD。不明なら空",
                            },
                            "entry": {"type": "string", "description": "いつ・どう入るか（寄り付き/大引け/◯営業日後）"},
                            "exit": {"type": "string", "description": "いつ・どう出るか"},
                            "mechanism": {"type": "string", "description": "なぜ動くか。需給・裁定・参加者行動で200字以内"},
                            "validation": {"type": "string", "description": "過去の同種事例での検証方法。150字以内"},
                            "reproducibility": {"type": "string", "enum": list(REPRODUCIBILITY)},
                            "horizon": {"type": "string", "enum": list(HORIZONS)},
                            "caveats": {"type": "string", "description": "地合い・空売り可否・流動性・先回りなど"},
                            "summary": {"type": "string", "description": "発信内容の要約。300字以内"},
                        },
                        "required": [
                            "source_index",
                            "headline",
                            "strategy_type",
                            "direction",
                            "long_leg",
                            "short_leg",
                            "tickers",
                            "trigger",
                            "event_date",
                            "entry",
                            "exit",
                            "mechanism",
                            "validation",
                            "reproducibility",
                            "horizon",
                            "caveats",
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
        header = (
            f"[{idx}] {item.platform} / {item.author} / {item.published_at.isoformat()} "
            f"/ 本文の種類: {item.text_origin}"
        )
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
                    "content": "次の発信から、条件に合う戦略アイデアだけを抽出してください。\n\n" + format_items(batch),
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
        return [idea for raw in parse_tool_output(data) if (idea := self._to_idea(raw, batch)) is not None]

    def _to_idea(self, raw: dict[str, Any], batch: list[ContentItem]) -> Idea | None:
        try:
            item = batch[int(raw.get("source_index", -1))]
        except (TypeError, ValueError, IndexError):
            return None
        strategy_type = str(raw.get("strategy_type") or "").strip()
        if strategy_type not in STRATEGY_TYPES:
            # 想定外の分類は「やり方に合わない」とみなして捨てる
            return None
        trigger = _s(raw.get("trigger"))
        long_leg, short_leg = _s(raw.get("long_leg")), _s(raw.get("short_leg"))
        if not trigger or not (long_leg or short_leg or raw.get("tickers")):
            return None
        tickers = [_s(t).upper() for t in (raw.get("tickers") or []) if _s(t)][:6]
        idea = Idea(
            item=item,
            headline=_s(raw.get("headline")) or trigger[:40],
            strategy_type=strategy_type,
            direction=_enum(raw.get("direction"), DIRECTIONS, "ウォッチ"),
            long_leg=long_leg,
            short_leg=short_leg,
            tickers=tickers,
            trigger=trigger,
            event_date=_parse_date(raw.get("event_date")),
            entry=_s(raw.get("entry")),
            exit=_s(raw.get("exit")),
            mechanism=_s(raw.get("mechanism")),
            validation=_s(raw.get("validation")),
            reproducibility=_enum(raw.get("reproducibility"), REPRODUCIBILITY, "低"),
            horizon=_enum(raw.get("horizon"), HORIZONS, "数日"),
            caveats=_s(raw.get("caveats")),
            summary=_s(raw.get("summary")),
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
            return [i for i in (payload.get("ideas") or []) if isinstance(i, dict)]
    return []


def is_holding(idea: Idea, holdings: list[tuple[str, str]]) -> bool:
    legs = f"{idea.long_leg} {idea.short_leg}"
    for code, name in holdings:
        if code and (code in idea.tickers or code in legs):
            return True
        if name and name in legs:
            return True
    return False


def _s(value: Any) -> str:
    return str(value or "").strip()


def _enum(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = _s(value)
    return text if text in allowed else default


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(_s(value)[:10])
    except ValueError:
        return None
