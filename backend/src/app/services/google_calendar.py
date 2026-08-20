"""The Google Calendar boundary.

Everything upstream talks to `CalendarGateway`, never to Google. That seam is what lets the
reconciler's whole failure surface — a revoked grant, a rate limit, an event that vanished
because the user deleted it by hand — be tested exhaustively against a fake, with no network,
no OAuth client, and no risk of writing to a real person's calendar from a test run.

Two decisions here are worth reading before the code.

**Event ids are ours, not Google's.** Google lets a client choose an event id, so it is
derived deterministically from the appointment and the user. A create that timed out *after*
Google committed it can therefore be retried without producing a second event: the retry
addresses the same id. The usual alternative — let Google assign an id and store it — has an
unfixable window between "Google created the event" and "we saved its id", and anything lost
in that window becomes an orphaned event nobody can delete.

**Nobody is added as an attendee.** The event is written separately to each participant's own
calendar. Listing the doctor as an attendee on the patient's copy would make Google *invite*
them, producing a second entry on their calendar alongside the one written directly, and an
invitation email competing with the clinic's own. Two direct writes are simpler and quieter
than one write plus an invitation.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.services.google_oauth import (
    AccessToken,
    GoogleAuthError,
    GoogleAuthRevoked,
    GoogleOAuthClient,
)

logger = logging.getLogger(__name__)

CALENDAR_API_ROOT = "https://www.googleapis.com/calendar/v3"

# Google's 403 covers both "you may not do this" and "you are doing it too fast". Only the
# second is worth retrying, and the difference is in the error reason, not the status code.
_RATE_LIMIT_REASONS = frozenset(
    {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded", "backenderror"}
)


class CalendarTransientError(Exception):
    """Google could not be reached, or asked us to come back later. Retry."""


class CalendarPermanentError(Exception):
    """Google rejected the request in a way that will not change on a retry."""


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """One calendar entry, in the terms Google needs.

    Deliberately free of ORM types: the gateway never sees an appointment row, so it can be
    faked without a database.
    """

    event_id: str
    summary: str
    description: str
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    location: str | None = None

    def to_google_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.event_id,
            "summary": self.summary,
            "description": self.description,
            "start": {"dateTime": _rfc3339(self.starts_at), "timeZone": self.time_zone},
            "end": {"dateTime": _rfc3339(self.ends_at), "timeZone": self.time_zone},
            # The clinic already sends its own reminder email, so the calendar contributes a
            # single short-notice popup rather than duplicating it.
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 60}],
            },
        }
        if self.location:
            body["location"] = self.location
        return body


class CalendarGateway(Protocol):
    """What the reconciler needs from a calendar provider."""

    async def upsert_event(
        self,
        *,
        refresh_token: str,
        calendar_id: str,
        event: CalendarEvent,
        exists: bool,
    ) -> None:
        """Make `event` the state of that calendar entry, creating it if necessary."""
        ...

    async def delete_event(self, *, refresh_token: str, calendar_id: str, event_id: str) -> None:
        """Remove the entry. Succeeds if it is already gone."""
        ...


def derive_event_id(appointment_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """A stable Google event id for one appointment on one user's calendar.

    Google requires base32hex characters (`0-9`, `a-v`), between 5 and 1024 of them. A
    SHA-256 of the two ids, base32hex-encoded, satisfies that and is collision-free while
    revealing nothing: the raw uuids are not recoverable from the id, so an event id visible
    in someone's calendar export is not an appointment identifier for this API.
    """
    digest = hashlib.sha256(f"{appointment_id}:{user_id}".encode()).digest()
    return base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()


class GoogleCalendarGateway:
    """Talks to the Calendar REST API with `httpx`.

    Access tokens are cached in process memory, keyed by a hash of the refresh token rather
    than the token itself, so the raw secret is never a dictionary key that could surface in
    a heap dump or a debugger's locals. The cache is why a worker pass over twenty events
    costs one token refresh instead of twenty.
    """

    def __init__(
        self,
        oauth: GoogleOAuthClient,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._oauth = oauth
        self._timeout = timeout_seconds
        self._transport = transport
        self._token_cache: dict[str, AccessToken] = {}

    async def upsert_event(
        self,
        *,
        refresh_token: str,
        calendar_id: str,
        event: CalendarEvent,
        exists: bool,
    ) -> None:
        token = await self._access_token(refresh_token)
        path = f"/calendars/{quote(calendar_id, safe='')}/events"
        body = event.to_google_body()

        if exists:
            # Believed to exist: update first. A 404 means it was deleted out from under us
            # — most often by the user tidying their own calendar — so it is recreated
            # rather than reported as an error.
            response = await self._request(
                "PUT", f"{path}/{quote(event.event_id, safe='')}", token, json=body
            )
            if response.status_code == 404:
                response = await self._request("POST", path, token, json=body)
        else:
            # Believed absent: insert first. A 409 means a previous attempt did commit
            # before we lost the response, so the event is updated into the desired shape.
            response = await self._request("POST", path, token, json=body)
            if response.status_code == 409:
                response = await self._request(
                    "PUT", f"{path}/{quote(event.event_id, safe='')}", token, json=body
                )

        _raise_for_status(response)

    async def delete_event(self, *, refresh_token: str, calendar_id: str, event_id: str) -> None:
        token = await self._access_token(refresh_token)
        response = await self._request(
            "DELETE",
            f"/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
            token,
        )
        # 404/410: the entry is already gone, which is precisely the requested end state.
        # Treating "not found" as failure here would dead-letter every cancellation of an
        # event a user had already deleted themselves.
        if response.status_code in (404, 410):
            return
        _raise_for_status(response)

    async def _access_token(self, refresh_token: str) -> str:
        key = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        cached = self._token_cache.get(key)
        if cached is not None and cached.is_valid_at(datetime.now(UTC)):
            return cached.token

        try:
            token = await self._oauth.refresh_access_token(refresh_token)
        except GoogleAuthError as exc:
            raise CalendarTransientError(str(exc)) from exc
        self._token_cache[key] = token
        return token.token

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                return await client.request(
                    method,
                    f"{CALENDAR_API_ROOT}{path}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=json,
                )
        except httpx.HTTPError as exc:
            raise CalendarTransientError(f"could not reach Google Calendar: {exc}") from exc


class InMemoryCalendarGateway:
    """Records what would have been written, instead of writing it.

    The default outside production. It keeps the reconciler, the worker and the API fully
    exercised with no Google project, and makes assertions about calendar state cheap in
    tests. Unlike a stub that silently does nothing, it holds the events, so a test can check
    that a cancellation actually removed one.
    """

    def __init__(self) -> None:
        self.events: dict[tuple[str, str], CalendarEvent] = {}
        self.deleted: list[tuple[str, str]] = []

    async def upsert_event(
        self,
        *,
        refresh_token: str,
        calendar_id: str,
        event: CalendarEvent,
        exists: bool,
    ) -> None:
        self.events[(calendar_id, event.event_id)] = event

    async def delete_event(self, *, refresh_token: str, calendar_id: str, event_id: str) -> None:
        self.events.pop((calendar_id, event_id), None)
        self.deleted.append((calendar_id, event_id))


def build_oauth_client(settings: Settings) -> GoogleOAuthClient | None:
    """The OAuth client, or `None` when Google is not configured.

    `None` rather than an exception: an unconfigured calendar is a supported state, not a
    broken one. Every other feature works without it.
    """
    if settings.google_client_id is None or settings.google_client_secret is None:
        return None
    return GoogleOAuthClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        redirect_uri=settings.google_redirect_uri,
        timeout_seconds=settings.google_api_timeout_seconds,
    )


def build_calendar_gateway(settings: Settings) -> CalendarGateway:
    """Choose a gateway from configuration.

    Falls back to the in-memory gateway when Google is not configured — which is safe here,
    unlike the equivalent fallback for the LLM, because nothing generated by this gateway is
    ever shown to a clinician as clinical content. The worst case is a calendar entry that
    was not created, and the sync row records exactly that.
    """
    oauth = build_oauth_client(settings)
    if oauth is None:
        return InMemoryCalendarGateway()
    return GoogleCalendarGateway(oauth, timeout_seconds=settings.google_api_timeout_seconds)


def _rfc3339(moment: datetime) -> str:
    """Google requires an offset; a naive datetime here would be silently misread."""
    if moment.tzinfo is None:
        raise ValueError("calendar times must be timezone-aware")
    return moment.isoformat()


def _raise_for_status(response: httpx.Response) -> None:
    """Turn a Calendar API response into success, a retry, or a dead letter."""
    if response.status_code < 400:
        return

    reason = _error_reason(response)

    if response.status_code == 401:
        # The access token was minted moments ago, so a 401 is not an expiry — the grant
        # itself is gone.
        raise GoogleAuthRevoked("Google rejected the access token; the grant is no longer valid.")
    if response.status_code == 429 or response.status_code >= 500:
        raise CalendarTransientError(f"Google Calendar returned {response.status_code} ({reason}).")
    if response.status_code == 403:
        if reason.lower() in _RATE_LIMIT_REASONS:
            raise CalendarTransientError(f"Google Calendar rate limit ({reason}).")
        raise CalendarPermanentError(f"Google Calendar refused the request ({reason}).")
    raise CalendarPermanentError(f"Google Calendar returned {response.status_code} ({reason}).")


def _error_reason(response: httpx.Response) -> str:
    """Google's error reason slug. The full body is never used: it echoes the request."""
    try:
        payload = response.json()
    except ValueError:
        return "unparseable error response"
    if not isinstance(payload, dict):
        return "unknown error"
    error = payload.get("error")
    if isinstance(error, dict):
        errors = error.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0].get("reason")
            if isinstance(first, str):
                return first
        message = error.get("message")
        if isinstance(message, str):
            return message[:200]
    return "unknown error"
