from __future__ import annotations

import os
import time
from typing import Any


WEBHOOK_ENV_BY_TYPE = {
    "po": "SLACK_WEBHOOK_PO",
    "ipo": "SLACK_WEBHOOK_IPO",
    "bunbai": "SLACK_WEBHOOK_BUNBAI",
    "cb": "SLACK_WEBHOOK_CB",
    "split": "SLACK_WEBHOOK_SPLIT",
    "system": "SLACK_WEBHOOK_SYSTEM",
}


class SlackNotifier:
    def __init__(self, dry_run: bool | None = None) -> None:
        self.dry_run = dry_run if dry_run is not None else os.getenv("SLACK_DRY_RUN") == "1"
        self.sent_messages: list[dict[str, Any]] = []

    def send(
        self,
        event_type: str,
        text: str,
        *,
        header: str | None = None,
        fields: list[str] | None = None,
        pdf_url: str | None = None,
    ) -> None:
        payload = build_payload(text, header=header, fields=fields, pdf_url=pdf_url)
        self.sent_messages.append({"type": event_type, "payload": payload})
        if self.dry_run:
            print(f"[SLACK_DRY_RUN:{event_type}] {text}")
            return

        env_name = WEBHOOK_ENV_BY_TYPE.get(event_type, "SLACK_WEBHOOK_SYSTEM")
        webhook_url = os.getenv(env_name)
        if not webhook_url:
            raise RuntimeError(f"Missing Slack webhook secret: {env_name}")

        post_payload(webhook_url, payload)

    def system(self, text: str) -> None:
        system_url = os.getenv("SLACK_WEBHOOK_SYSTEM")
        if not system_url and not self.dry_run:
            print(f"[SYSTEM] {text}")
            return
        self.send("system", text, header="システム通知")


def build_payload(
    text: str,
    *,
    header: str | None = None,
    fields: list[str] | None = None,
    pdf_url: str | None = None,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if header:
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": header[:150]}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    if fields:
        blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": item} for item in fields]})
    if pdf_url:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"開示PDF: <{pdf_url}|link>"}})
    return {"text": text, "blocks": blocks}


def post_payload(webhook_url: str, payload: dict[str, Any]) -> None:
    import requests

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(webhook_url, json=payload, timeout=15)
            response.raise_for_status()
            return
        except Exception as exc:  # pragma: no cover - network dependent
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError("Slack notification failed after retries") from last_error
