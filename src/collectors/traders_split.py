from __future__ import annotations

from datetime import date, timedelta
import os
import re
from typing import Any

from bs4 import BeautifulSoup

from src.collectors.utils import load_json_cache, normalize_code, request_get, save_json_cache
from src.core.dateparse import parse_date_token


TRADERS_SPLIT_URL = os.getenv("TRADERS_SPLIT_URL", "https://www.traders.co.jp/stock_data/split")
CACHE_NAME = "traders_splits.json"


def fetch_traders_splits(force: bool = False) -> dict[str, list[dict[str, Any]]]:
    cached = None if force else load_json_cache(CACHE_NAME, max_age=timedelta(hours=6))
    if cached is not None:
        return cached
    try:
        records = parse_traders_split_html(request_get(TRADERS_SPLIT_URL).decode("utf-8", errors="ignore"))
        if not records:
            raise RuntimeError("Traders split parser returned no records")
        save_json_cache(CACHE_NAME, records)
        return records
    except Exception as exc:
        fallback = load_json_cache(CACHE_NAME)
        if fallback is not None:
            return fallback
        raise RuntimeError("Traders split data is unavailable and no cache exists") from exc


def parse_traders_split_html(html: str) -> dict[str, list[dict[str, Any]]]:
    soup = BeautifulSoup(html, "html.parser")
    records: dict[str, list[dict[str, Any]]] = {}
    for heading in soup.select(".zone_title_large"):
        year_match = re.fullmatch(r"\s*(20\d{2})年\s*", heading.get_text(" ", strip=True))
        if not year_match:
            continue
        year = int(year_match.group(1))
        table = heading.find_next("table")
        if table is None:
            continue
        for row in table.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) < 4 or "権利取最終日" in cells[0]:
                continue
            code = normalize_code(cells[1])
            rights_final = parse_date_token(cells[0], default_year=year)
            effective = parse_date_token(cells[3], default_year=year)
            ratio_match = re.search(r"1\s*(?:→|->|:|：|対)\s*([0-9]+(?:\.[0-9]+)?)", cells[2])
            if not code or not rights_final or not effective or not ratio_match:
                continue
            if effective < rights_final:
                effective = date(year + 1, effective.month, effective.day)
            ratio = ratio_match.group(1)
            if "." in ratio:
                ratio = ratio.rstrip("0").rstrip(".")
            records.setdefault(code, []).append(
                {
                    "code": code,
                    "name": cells[1].split("(", 1)[0].strip(),
                    "rights_final_date": rights_final.isoformat(),
                    "ratio": ratio,
                    "effective_date": effective.isoformat(),
                    "source_url": TRADERS_SPLIT_URL,
                }
            )
    return records
