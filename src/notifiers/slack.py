from __future__ import annotations

import os
import time
from typing import Any, Callable


WEBHOOK_ENV_BY_TYPE = {
    "po": "SLACK_WEBHOOK_PO",
    "ipo": "SLACK_WEBHOOK_IPO",
    "bunbai": "SLACK_WEBHOOK_BUNBAI",
    "cb": "SLACK_WEBHOOK_CB",
    "split": "SLACK_WEBHOOK_SPLIT",
    "system": "SLACK_WEBHOOK_SYSTEM",
}


class SlackDeliveryUncertain(RuntimeError):
    """The request may have reached Slack; do not automatically repeat it."""


class SlackNotifier:
    def __init__(self, dry_run: bool | None = None) -> None:
        self.dry_run = dry_run if dry_run is not None else os.getenv("SLACK_DRY_RUN") == "1"
        self.sent_messages: list[dict[str, Any]] = []
        self.success_count = 0
        self.failure_count = 0

    def send(
        self,
        event_type: str,
        text: str,
        *,
        header: str | None = None,
        fields: list[str] | None = None,
        pdf_url: str | None = None,
        text_factory: Callable[[], str] | None = None,
    ) -> None:
        payload = build_payload(text, header=header, fields=fields, pdf_url=pdf_url)
        self.sent_messages.append({"type": event_type, "payload": payload})
        if self.dry_run:
            print(f"[SLACK_DRY_RUN:{event_type}] {text}")
            self.success_count += 1
            return

        env_name = WEBHOOK_ENV_BY_TYPE.get(event_type, "SLACK_WEBHOOK_SYSTEM")
        webhook_url = os.getenv(env_name)
        if not webhook_url:
            self.failure_count += 1
            raise RuntimeError(f"Missing Slack webhook secret: {env_name}")
        try:
            def current_payload() -> dict[str, Any]:
                if text_factory is not None:
                    payload.update(build_payload(text_factory(), header=header, fields=fields, pdf_url=pdf_url))
                return payload

            post_payload(webhook_url, payload, payload_factory=current_payload)
        except Exception:
            self.failure_count += 1
            raise
        self.success_count += 1

    def system(self, text: str) -> None:
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


def post_payload(webhook_url: str, payload: dict[str, Any], *,
                 payload_factory: Callable[[], dict[str, Any]] | None = None) -> None:
    import requests

    for attempt in range(3):
        retry_after = 2**attempt
        current = payload_factory() if payload_factory is not None else payload
        try:
            response = requests.post(webhook_url, json=current, timeout=15)
            response.raise_for_status()
            return
        except requests.ConnectTimeout:
            # requests documents ConnectTimeout as safe to retry: no request
            # has reached Slack yet. ReadTimeout/ConnectionError are ambiguous.
            pass
        except (requests.ReadTimeout, requests.ConnectionError):
            raise SlackDeliveryUncertain("Slack response was lost; verify the message before retrying") from None
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is None or status >= 500:
                raise SlackDeliveryUncertain("Slack returned a server error; verify delivery before retrying") from None
            if status != 429:
                raise RuntimeError("Slack rejected the notification") from None
            try:
                retry_after = max(retry_after, float(exc.response.headers.get("Retry-After", retry_after)))
            except (TypeError, ValueError):
                pass
            if retry_after > 30:
                raise RuntimeError("Slack rate limit requires a later workflow retry") from None
        except requests.RequestException:
            raise SlackDeliveryUncertain("Slack request failed with an uncertain delivery result") from None
        except Exception:  # pragma: no cover - unexpected transport failure
            raise SlackDeliveryUncertain("Slack delivery could not be confirmed") from None
        if attempt < 2:
            time.sleep(retry_after)
    raise RuntimeError("Slack notification failed after retries") from None
