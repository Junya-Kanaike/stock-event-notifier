from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import os
from typing import Any
from urllib.parse import urlencode

from src.collectors.utils import load_json_cache, request_get, save_json_cache
from src.core.bizday import JST, as_date, is_business_day, prev_business_day


YAHOO_CHART_URL = os.getenv("YAHOO_CHART_URL", "https://query2.finance.yahoo.com/v8/finance/chart")
CACHE_NAME = "yahoo_closes.json"
MARKET_CLOSE = time(15, 30)


def reference_close_date(announced_at: datetime, *, as_of: datetime | None = None) -> tuple[date, str]:
    announced_jst = announced_at.astimezone(JST) if announced_at.tzinfo else announced_at.replace(tzinfo=JST)
    current = as_of or datetime.now(JST)
    current = current.astimezone(JST) if current.tzinfo else current.replace(tzinfo=JST)
    announced_day = announced_jst.date()
    if not is_business_day(announced_day):
        return prev_business_day(announced_day), "previous_close_non_business_day"
    if current.date() < announced_day or (current.date() == announced_day and current.time() < MARKET_CLOSE):
        return prev_business_day(announced_day), "previous_close_pre_close"
    return announced_day, "announcement_close"


def fetch_close_on_or_before(code: str, target_date: date | str, *, force: bool = False) -> dict[str, Any]:
    target = as_date(target_date)
    cache = load_json_cache(CACHE_NAME) or {}
    cache_key = f"{code}:{target.isoformat()}"
    if not force and cache_key in cache and cache[cache_key].get("date") == target.isoformat():
        return dict(cache[cache_key])

    symbol = f"{code}.T"
    period1 = datetime.combine(target - timedelta(days=14), time.min, tzinfo=JST)
    period2 = datetime.combine(target + timedelta(days=2), time.min, tzinfo=JST)
    query = urlencode(
        {
            "period1": int(period1.timestamp()),
            "period2": int(period2.timestamp()),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
    )
    payload = request_get(f"{YAHOO_CHART_URL}/{symbol}?{query}", timeout=20)
    import json

    document = json.loads(payload.decode("utf-8"))
    result = ((document.get("chart") or {}).get("result") or [None])[0]
    if not result:
        error = (document.get("chart") or {}).get("error")
        raise RuntimeError(f"Yahoo price result unavailable: {error}")
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    candidates: list[tuple[date, float]] = []
    for stamp, close in zip(timestamps, quote):
        if close is None:
            continue
        trading_day = datetime.fromtimestamp(int(stamp), timezone.utc).astimezone(JST).date()
        if trading_day == target:
            candidates.append((trading_day, float(close)))
    if not candidates:
        raise RuntimeError(f"Yahoo close unavailable for {symbol} on {target.isoformat()}")
    trading_day, close = max(candidates, key=lambda item: item[0])
    record = {
        "code": code,
        "symbol": symbol,
        "date": trading_day.isoformat(),
        "close_yen": close,
        "source": "Yahoo Finance chart (raw close)",
        "requested_date": target.isoformat(),
    }
    cache[cache_key] = record
    save_json_cache(CACHE_NAME, cache)
    return record
