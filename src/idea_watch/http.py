from __future__ import annotations

import time
from typing import Any

import requests


DEFAULT_TIMEOUT = 30
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class ApiError(RuntimeError):
    def __init__(self, service: str, status: int, detail: str = "") -> None:
        # レスポンス本文はログに出さない（publicリポジトリのActionsログ対策）
        super().__init__(f"{service} API error: HTTP {status}")
        self.service = service
        self.status = status
        self.detail = detail


def request_json(
    method: str,
    url: str,
    *,
    service: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    retries: int = 3,
    timeout: int = DEFAULT_TIMEOUT,
    session: Any = None,
) -> dict[str, Any]:
    client = session or requests
    for attempt in range(retries + 1):
        response = client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )
        status = response.status_code
        if status in RETRY_STATUSES and attempt < retries:
            wait = _retry_after(response, attempt)
            time.sleep(wait)
            continue
        if status >= 400:
            raise ApiError(service, status, response.text[:500])
        if not response.content:
            return {}
        return response.json()
    raise ApiError(service, -1)  # pragma: no cover


def _retry_after(response: Any, attempt: int) -> float:
    raw = response.headers.get("retry-after") if hasattr(response, "headers") else None
    try:
        if raw is not None:
            return min(float(raw), 60.0)
    except ValueError:
        pass
    return float(2 ** (attempt + 1))
