"""Optional outbound notification providers for deadline reminders.

In-app reminders never depend on this module. The only external provider is
the official WhatsApp Business Platform (Cloud API). It is used only when all
of its environment variables are set and the user has opted in, and a message
counts as sent only when the Cloud API accepts it and returns a message id.
Acceptance is not a delivery receipt: receipts arrive through Meta webhooks,
which TaskMate does not consume yet.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

WHATSAPP_ENV_VARS = (
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_TEMPLATE_NAME",
)


@dataclass
class DeliveryResult:
    accepted: bool
    provider_message_id: str | None = None
    retryable: bool = False
    error: str | None = None


class NotificationProvider(Protocol):
    channel: str

    def is_configured(self) -> bool: ...

    def send(self, *, to: str, template_params: list[str]) -> DeliveryResult: ...


class WhatsAppCloudProvider:
    """Sends an approved message template through the WhatsApp Cloud API.

    Business-initiated messages outside a 24-hour customer service window must
    use a template that Meta has approved, so free-form text is never sent.
    The template body is expected to take two variables: the task title and
    the local due time.
    """

    channel = "whatsapp"

    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(timeout=15.0)

    @staticmethod
    def missing_settings() -> list[str]:
        return [name for name in WHATSAPP_ENV_VARS if not os.getenv(name)]

    def is_configured(self) -> bool:
        return not self.missing_settings()

    def send(self, *, to: str, template_params: list[str]) -> DeliveryResult:
        if not self.is_configured():
            return DeliveryResult(accepted=False, error="WhatsApp provider is not configured.")

        version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v21.0")
        phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
        payload = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "template",
            "template": {
                "name": os.environ["WHATSAPP_TEMPLATE_NAME"],
                "language": {"code": os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en")},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": value} for value in template_params],
                    }
                ],
            },
        }
        try:
            response = self._http.post(
                f"https://graph.facebook.com/{version}/{phone_number_id}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {os.environ['WHATSAPP_ACCESS_TOKEN']}"},
            )
        except httpx.RequestError as exc:
            logger.warning("WhatsApp request failed (%s)", type(exc).__name__)
            return DeliveryResult(accepted=False, retryable=True, error=type(exc).__name__)

        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code == 200:
            messages = data.get("messages") if isinstance(data, dict) else None
            message_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
            if message_id:
                return DeliveryResult(accepted=True, provider_message_id=message_id)
            return DeliveryResult(accepted=False, error="WhatsApp returned no message id.")

        error = data.get("error", {}) if isinstance(data, dict) else {}
        code = error.get("code") if isinstance(error, dict) else None
        logger.warning("WhatsApp rejected a reminder (HTTP %s, code %s)", response.status_code, code)
        return DeliveryResult(
            accepted=False,
            retryable=response.status_code == 429 or response.status_code >= 500,
            error=f"HTTP {response.status_code}" + (f" (code {code})" if code else ""),
        )
