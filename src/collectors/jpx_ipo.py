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
    columns: dict[str, int] | None = None
    for row in soup.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if not cells:
            continue
        if any("上場日" in cell for cell in cells) and any("コード" in cell for cell in cells):
            columns = _column_indexes(cells)
            continue

        code_text = _cell(cells, columns, "code") if columns else ""
        date_text = _cell(cells, columns, "date") if columns else ""
        code = normalize_code(code_text) or _code_from_cells(cells)
        dates = find_dates(date_text, default_year=year) if date_text else find_dates(" ".join(cells), default_year=year)
        if not code or not dates:
            continue
        name = _cell(cells, columns, "name") if columns else _guess_name(cells, code)
        market = _cell(cells, columns, "market") if columns else ""
        records.append(
            {
                "code": code,
                "name": name,
                "market": market,
                "listing_date": dates[0].isoformat(),
                "source_url": IPO_URL,
            }
        )
    return records


def _column_indexes(cells: list[str]) -> dict[str, int]:
    def find(label: str) -> int:
        return next((index for index, cell in enumerate(cells) if label in cell), -1)

    return {
        "date": find("上場日"),
        "name": find("会社名"),
        "code": find("コード"),
        "market": find("市場区分"),
    }


def _cell(cells: list[str], columns: dict[str, int] | None, key: str) -> str:
    index = columns.get(key, -1) if columns else -1
    return cells[index] if 0 <= index < len(cells) else ""


def _code_from_cells(cells: list[str]) -> str | None:
    for cell in cells:
        compact = re.sub(r"\s+", "", cell).upper()
        code = normalize_code(compact)
        if code and compact in {code, f"{code}0"}:
            return code
    return None


def _guess_name(cells: list[str], code: str) -> str:
    for index, cell in enumerate(cells):
        if code in cell and index + 1 < len(cells):
            return cells[index + 1]
    for cell in cells:
        if re.search(r"[一-龥ぁ-んァ-ヶー]", cell) and code not in cell:
            return cell
    return ""
