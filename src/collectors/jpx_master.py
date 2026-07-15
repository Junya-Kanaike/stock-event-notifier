from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from src.collectors.utils import load_json_cache, normalize_code, request_get, save_json_cache, workbook_rows


MASTER_URL = os.getenv("JPX_MASTER_URL", "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls")
CACHE_NAME = "jpx_master.json"


def fetch_master(force: bool = False) -> dict[str, dict[str, str]]:
    cached = None if force else load_json_cache(CACHE_NAME, max_age=timedelta(days=40))
    if cached is not None:
        return cached
    try:
        records = parse_master_excel(request_get(MASTER_URL))
        if not records:
            raise RuntimeError("JPX master parser returned no records")
        save_json_cache(CACHE_NAME, records)
        return records
    except Exception as exc:
        fallback = load_json_cache(CACHE_NAME)
        if fallback:
            return fallback
        raise RuntimeError("JPX master data is unavailable and no usable cache exists") from exc


def parse_master_excel(content: bytes) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    header: list[str] | None = None
    for raw_row in workbook_rows(content):
        row = ["" if cell is None else str(cell).strip() for cell in raw_row]
        if not any(row):
            continue
        if "コード" in row and any("銘柄名" in cell for cell in row):
            header = row
            continue
        if not header:
            continue
        mapped = _row_to_dict(header, row)
        code = normalize_code(mapped.get("コード"))
        if not code:
            continue
        name = mapped.get("銘柄名") or mapped.get("銘柄名（英語）") or ""
        market = mapped.get("市場・商品区分") or mapped.get("市場区分") or ""
        records[code] = {"name": name, "market": _normalize_market(market)}
    return records


def lookup_master(master: dict[str, dict[str, str]], code: str, fallback_name: str = "") -> dict[str, str]:
    item = master.get(code, {})
    return {
        "name": item.get("name") or fallback_name,
        "market": item.get("market") or "取得失敗",
    }


def _row_to_dict(header: list[str], row: list[str]) -> dict[str, Any]:
    return {header[index]: row[index] if index < len(row) else "" for index in range(len(header))}


def _normalize_market(value: str) -> str:
    if "プライム" in value:
        return "プライム"
    if "スタンダード" in value:
        return "スタンダード"
    if "グロース" in value:
        return "グロース"
    return value or "取得失敗"
