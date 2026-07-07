from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Any

from bs4 import BeautifulSoup

from src.collectors.utils import load_json_cache, normalize_code, request_get, save_json_cache
from src.core.dateparse import find_dates


IPO_URL = os.getenv("JPX_IPO_URL", "https://www.jpx.co.jp/listing/stocks/new/index.html")
CACHE_NAME = "jpx_ipo.json"


def fetch_ipos(force: bool = False) -> list[dict[str, Any]]:
    cached = None if force else load_json_cache(CACHE_NAME, max_age=timedelta(days=2))
    if cached is not None:
        return cached
    try:
        html = request_get(IPO_URL).decode("utf-8", errors="ignore")
        records = parse_ipo_html(html)
        save_json_cache(CACHE_NAME, records)
        return records
    except Exception:
        return load_json_cache(CACHE_NAME) or []


def parse_ipo_html(html: str, default_year: int | None = None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    year = default_year or date.today().year
    records: list[dict[str, Any]] = []
    for row in soup.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        joined = " ".join(cells)
        code = normalize_code(joined)
        dates = find_dates(joined, default_year=year)
        if not code or not dates:
            continue
        name = _guess_name(cells, code)
        records.append({"code": code, "name": name, "listing_date": dates[0].isoformat(), "source_url": IPO_URL})
    return records


def _guess_name(cells: list[str], code: str) -> str:
    for index, cell in enumerate(cells):
        if code in cell and index + 1 < len(cells):
            return cells[index + 1]
    for cell in cells:
        if re.search(r"[一-龥ぁ-んァ-ヶー]", cell) and code not in cell:
            return cell
    return ""
