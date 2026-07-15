from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from src.core.store import REPO_ROOT


USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "event-driven-slack-bot/1.0 (+https://github.com/; personal use)",
)
DEFAULT_TIMEOUT = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
CACHE_DIR = REPO_ROOT / "state" / "cache"
CACHE_FORMAT_VERSION = 1


def normalize_code(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).upper()
    # TDnet sometimes appends a category zero (72030 -> 7203). Do not
    # truncate arbitrary five-digit instruments such as 92015.
    match = re.search(r"(?<![0-9A-Z])(?P<code>\d{3}[A-Z]|\d{4})0?(?![0-9A-Z])", text)
    return match.group("code") if match else None


def request_get(url: str, *, timeout: int = DEFAULT_TIMEOUT, retries: int = 2) -> bytes:
    import requests

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            response.raise_for_status()
            return response.content
        except Exception as exc:  # pragma: no cover - network dependent
            last_error = exc
            if attempt < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"GET failed: {url}") from last_error


def cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def load_json_cache(name: str, max_age: timedelta | None = None) -> Any | None:
    target = cache_path(name)
    if not target.exists():
        return None

    try:
        with target.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None

    if isinstance(payload, dict) and payload.get("cache_version") == CACHE_FORMAT_VERSION and "data" in payload:
        if max_age:
            try:
                fetched_at = datetime.fromisoformat(str(payload["fetched_at"]).replace("Z", "+00:00"))
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                return None
            if datetime.now(timezone.utc) - fetched_at.astimezone(timezone.utc) > max_age:
                return None
        return payload["data"]

    # Legacy caches used git checkout mtimes as their TTL. A checkout refreshes
    # those mtimes, so legacy content must be refreshed whenever an age limit is
    # requested. It remains available as an explicit stale fallback.
    return None if max_age else payload


def save_json_cache(name: str, data: Any) -> None:
    if os.getenv("CACHE_READ_ONLY") == "1":
        return
    target = cache_path(name)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        payload = {
            "cache_version": CACHE_FORMAT_VERSION,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(target)


def workbook_rows(content: bytes) -> Iterable[list[Any]]:
    try:
        import xlrd

        book = xlrd.open_workbook(file_contents=content)
        for sheet in book.sheets():
            for row_index in range(sheet.nrows):
                yield sheet.row_values(row_index)
        return
    except Exception:
        pass

    import openpyxl

    book = openpyxl.load_workbook(BytesIO(content), data_only=True, read_only=True)
    for sheet in book.worksheets:
        for row in sheet.iter_rows(values_only=True):
            yield list(row)


def absolute_url(base_url: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base_url, href)
