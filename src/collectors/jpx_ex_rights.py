from __future__ import annotations

from datetime import date, datetime, timedelta
import os
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup

from src.collectors.utils import absolute_url, load_json_cache, normalize_code, request_get, save_json_cache, workbook_rows
from src.core.dateparse import parse_date_token


EX_RIGHTS_URL = os.getenv("JPX_EX_RIGHTS_URL", "https://www.jpx.co.jp/listing/others/ex-rights/index.html")
CACHE_NAME = "jpx_ex_rights.json"
EXCEL_EPOCH = date(1899, 12, 30)


def fetch_ex_rights(force: bool = False) -> dict[str, dict[str, Any]]:
    cached = None if force else load_json_cache(CACHE_NAME, max_age=timedelta(days=1))
    if cached is not None:
        return cached
    try:
        page = request_get(EX_RIGHTS_URL).decode("utf-8", errors="ignore")
        excel_url = find_latest_excel_url(page, EX_RIGHTS_URL)
        records = parse_ex_rights_rows(workbook_rows(request_get(excel_url)))
        save_json_cache(CACHE_NAME, records)
        return records
    except Exception as exc:
        fallback = load_json_cache(CACHE_NAME)
        if fallback is not None:
            return fallback
        raise RuntimeError("JPX ex-rights data is unavailable and no cache exists") from exc


def find_latest_excel_url(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if re.search(r"/\d{8}\.xlsx?$", href, re.I):
            candidates.append(absolute_url(base_url, href))
    if not candidates:
        raise RuntimeError("JPX ex-rights Excel link not found")
    return sorted(candidates)[-1]


def parse_ex_rights_rows(rows: Iterable[list[Any]]) -> dict[str, dict[str, Any]]:
    columns: dict[str, int] | None = None
    records: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = ["" if value is None else value for value in raw_row]
        labels = [re.sub(r"\s+", "", str(value)) for value in row]
        if "銘柄コード" in labels and any("権利落日" in label and "普通取引" in label for label in labels):
            columns = {
                "code": labels.index("銘柄コード"),
                "record_date": labels.index("基準日") if "基準日" in labels else -1,
                "ex_date": next(i for i, label in enumerate(labels) if "権利落日" in label and "普通取引" in label),
                "name": labels.index("銘柄略称") if "銘柄略称" in labels else -1,
                "market": labels.index("市場") if "市場" in labels else -1,
                "note": labels.index("備考") if "備考" in labels else -1,
            }
            continue
        if not columns:
            continue
        note = str(_cell(row, columns["note"]))
        if "分割" not in note:
            continue
        code = normalize_code(_cell(row, columns["code"]))
        ex_date = _excel_date(_cell(row, columns["ex_date"]))
        record_date = _excel_date(_cell(row, columns["record_date"]))
        if not code or not ex_date:
            continue
        records[code] = {
            "code": code,
            "name": str(_cell(row, columns["name"])).strip(),
            "market": str(_cell(row, columns["market"])).strip(),
            "ex_right_date": ex_date.isoformat(),
            "record_date": record_date.isoformat() if record_date else None,
            "rights_final_date": _previous_weekday(ex_date).isoformat(),
            "source_url": EX_RIGHTS_URL,
        }
    return records


def _cell(row: list[Any], index: int) -> Any:
    return row[index] if 0 <= index < len(row) else ""


def _excel_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return EXCEL_EPOCH + timedelta(days=int(value))
    return parse_date_token(str(value))


def _previous_weekday(day: date) -> date:
    from src.core.bizday import prev_business_day

    return prev_business_day(day)
