from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Any

from bs4 import BeautifulSoup

from src.collectors.utils import load_json_cache, normalize_code, request_get, save_json_cache
from src.core.dateparse import find_dates


BUNBAI_URL = os.getenv("JPX_BUNBAI_URL", "https://www.jpx.co.jp/markets/equities/off-auction-distro/index.html")
CACHE_NAME = "jpx_bunbai.json"


def fetch_bunbai(force: bool = False) -> list[dict[str, Any]]:
    cached = None if force else load_json_cache(CACHE_NAME, max_age=timedelta(days=2))
    if cached is not None:
        return cached
    try:
        html = request_get(BUNBAI_URL).decode("utf-8", errors="ignore")
        records = parse_bunbai_html(html)
        save_json_cache(CACHE_NAME, records)
        return records
    except Exception:
        return load_json_cache(CACHE_NAME) or []


def parse_bunbai_html(html: str, default_year: int | None = None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    year = default_year or date.today().year
    records: list[dict[str, Any]] = []
    columns: dict[str, int] | None = None
    for row in soup.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if not cells:
            continue
        if any("実施日" in cell for cell in cells) and any("銘柄名" in cell for cell in cells):
            columns = {
                "date": next(index for index, cell in enumerate(cells) if "実施日" in cell),
                "issue": next(index for index, cell in enumerate(cells) if "銘柄名" in cell),
            }
            continue

        date_text = _cell(cells, columns, "date") if columns else cells[0]
        issue_text = _cell(cells, columns, "issue") if columns else " ".join(cells)
        code = normalize_code(issue_text)
        dates = find_dates(date_text, default_year=year)
        if not code or not dates:
            continue
        records.append(
            {
                "code": code,
                "name": _clean_issue_name(issue_text, code),
                "execution_date": dates[0].isoformat(),
                "source_url": BUNBAI_URL,
            }
        )
    return records


def _cell(cells: list[str], columns: dict[str, int] | None, key: str) -> str:
    index = columns.get(key, -1) if columns else -1
    return cells[index] if 0 <= index < len(cells) else ""


def _clean_issue_name(value: str, code: str) -> str:
    return re.sub(rf"\s*株式?\s*[（(]{re.escape(code)}[）)]\s*$", "", value).strip()


def _guess_name(cells: list[str], code: str) -> str:
    for index, cell in enumerate(cells):
        if code in cell and index + 1 < len(cells):
            return cells[index + 1]
    for cell in cells:
        if re.search(r"[一-龥ぁ-んァ-ヶー]", cell) and code not in cell:
            return cell
    return ""
