from __future__ import annotations

import os
import re
from datetime import timedelta

from bs4 import BeautifulSoup

from src.collectors.utils import absolute_url, load_json_cache, normalize_code, request_get, save_json_cache, workbook_rows


MARGIN_URL = os.getenv("JPX_MARGIN_URL", "https://www.jpx.co.jp/listing/others/margin/index.html")
CACHE_NAME = "jpx_margin.json"


def fetch_margin(force: bool = False) -> dict[str, str]:
    cached = None if force else load_json_cache(CACHE_NAME, max_age=timedelta(days=3))
    if cached is not None:
        return cached
    try:
        page = request_get(MARGIN_URL).decode("utf-8", errors="ignore")
        excel_url = find_margin_excel_url(page, MARGIN_URL)
        records = parse_margin_excel(request_get(excel_url))
        if not records:
            raise RuntimeError("JPX margin parser returned no records")
        save_json_cache(CACHE_NAME, records)
        return records
    except Exception as exc:
        fallback = load_json_cache(CACHE_NAME)
        if fallback:
            return fallback
        raise RuntimeError("JPX margin data is unavailable and no usable cache exists") from exc


def find_margin_excel_url(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[int, str]] = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        label = link.get_text(" ", strip=True)
        if not label and link.parent:
            label = link.parent.get_text(" ", strip=True)
        if not re.search(r"\.(xls|xlsx)$", href, re.I):
            continue
        score = 0
        if "制度信用" in label or "制度信用" in href:
            score += 2
        if "貸借" in label or "貸借" in href:
            score += 2
        if "銘柄" in label:
            score += 1
        candidates.append((score, absolute_url(base_url, href)))
    if not candidates:
        raise RuntimeError("JPX margin Excel link not found")
    return sorted(candidates, reverse=True)[0][1]


def parse_margin_excel(content: bytes) -> dict[str, str]:
    records: dict[str, str] = {}
    for row in workbook_rows(content):
        cells = ["" if cell is None else str(cell).strip() for cell in row]
        joined = " ".join(cells)
        code = normalize_code(joined)
        if not code:
            continue
        if "貸借" in joined:
            records[code] = "貸借"
        elif "制度信用" in joined or "信用" in joined:
            records.setdefault(code, "信用")
    return records


def lookup_margin(margin: dict[str, str], code: str) -> str:
    if not margin:
        return "取得失敗"
    return margin.get(code) or "対象外"
