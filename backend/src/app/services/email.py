"""Email delivery.

The rest of the system talks to `EmailSender`, never to a provider directly. That boundary is
what lets the worker's retry behaviour be tested exhaustively — a fake sender can fail on
demand — without a network, an API key, or any risk of mailing a real person from a test.

`ConsoleEmailSender` is the default everywhere except production, so a misconfigured
development environment logs messages rather than sending them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """The provider did not accept the message.

    Raised for anything the worker should retry: a network failure, a timeout, or a 5xx. A
    permanent rejection (a malformed address) also raises, and is separated from transient
    failures only by the retry budget running out.
    """


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to_address: str
    to_name: str
    subject: str
    body: str


class EmailSender(Protocol):
    """What the notification worker needs from an email provider."""

    async def send(self, message: EmailMessage) -> None:
        """Deliver one message, or raise `EmailDeliveryError`."""
        ...


class ConsoleEmailSender:
    """Logs the message instead of sending it.

    Used in development and tests. Deliberately logs the full body: locally that is the point,
    and it never runs in production where the body would be patient data in a log aggregator.
    """

    async def send(self, message: EmailMessage) -> None:
        # The detail goes in the message itself, not only in `extra`: the development log
        # format renders no extra fields, so a structured-only line would show "email
        # (console)" and nothing else — defeating the entire purpose of this sender. The
        # structured fields are kept as well, for the JSON format used in production.
        logger.info(
            "email (console) to %s | %s\n%s",
            message.to_address,
            message.subject,
            message.body,
            extra={
                "to": message.to_address,
                "subject": message.subject,
                "body": message.body,
            },
        )


class SendGridEmailSender:
    """Delivers through SendGrid's v3 API.

    A single `httpx` call rather than a provider SDK: the request is a dozen lines, and an SDK
    would be another dependency to pin, audit and keep current for no gain.
    """

    ENDPOINT = "https://api.sendgrid.com/v3/mail/send"

    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
        from_name: str,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._from_address = from_address
        self._from_name = from_name
        self._timeout = timeout_seconds
        # Injectable so the request shape can be asserted against a mock transport.
        self._client = client

    async def send(self, message: EmailMessage) -> None:
        payload = {
            "personalizations": [{"to": [{"email": message.to_address, "name": message.to_name}]}],
            "from": {"email": self._from_address, "name": self._from_name},
            "subject": message.subject,
            "content": [{"type": "text/plain", "value": message.body}],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            if self._client is not None:
                response = await self._client.post(
                    self.ENDPOINT, json=payload, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(self.ENDPOINT, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # Network-level failures are exactly what the outbox exists to survive.
            raise EmailDeliveryError(f"could not reach the email provider: {exc}") from exc

        if response.status_code >= 400:
            # The body can carry the provider's reason; truncated so a verbose error cannot
            # bloat the stored `last_error`.
            raise EmailDeliveryError(
                f"email provider rejected the message ({response.status_code}): "
                f"{response.text[:200]}"
            )


def build_sender(settings: Settings) -> EmailSender:
    """Choose a sender from configuration.

    A `sendgrid` provider with no API key is a configuration error worth failing loudly on:
    silently falling back to the console would look like success while no patient ever
    receives anything.
    """
    if settings.email_provider == "sendgrid":
        if settings.email_api_key is None:
            raise ValueError(
                "EMAIL_PROVIDER is 'sendgrid' but EMAIL_API_KEY is not set. "
                "Set the key, or use EMAIL_PROVIDER=console for local development."
            )
        return SendGridEmailSender(
            api_key=settings.email_api_key.get_secret_value(),
            from_address=settings.email_from,
            from_name=settings.email_from_name,
            timeout_seconds=settings.email_timeout_seconds,
        )
    return ConsoleEmailSender()
