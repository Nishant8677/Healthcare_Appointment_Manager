"""The Google Calendar HTTP boundary, driven by a mock transport.

No network and no Google project: `httpx.MockTransport` answers every request, so the exact
requests this code makes — and the exact way it reads the answers — are asserted directly.
That matters more here than usual, because the interesting behaviour is entirely in how
status codes are classified, and a misclassified 403 means either a permanent failure retried
forever or a rate limit dead-lettered on first contact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.services.google_calendar import (
    CalendarEvent,
    CalendarPermanentError,
    CalendarTransientError,
    GoogleCalendarGateway,
    derive_event_id,
)
from app.services.google_oauth import GoogleAuthRevoked, GoogleOAuthClient, authorization_url

STARTS = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def make_event(event_id: str = "abc12") -> CalendarEvent:
    return CalendarEvent(
        event_id=event_id,
        summary="Appointment with Dr Test",
        description="Cardiology",
        starts_at=STARTS,
        ends_at=STARTS + timedelta(minutes=30),
        time_zone="UTC",
    )


def build_gateway(handler: Any) -> tuple[GoogleCalendarGateway, list[httpx.Request]]:
    """A gateway whose every HTTP call is answered by `handler` and recorded."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "fresh-access-token", "expires_in": 3600}
            )
        return handler(request)

    transport = httpx.MockTransport(record)
    oauth = GoogleOAuthClient(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="http://testserver/calendar/callback",
        transport=transport,
    )
    return GoogleCalendarGateway(oauth, transport=transport), seen


def calendar_requests(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in seen if request.url.host == "www.googleapis.com"]


# --------------------------------------------------------------------------- event ids


def test_event_ids_are_stable_and_unique_per_calendar() -> None:
    import uuid

    appointment, patient, doctor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    assert derive_event_id(appointment, patient) == derive_event_id(appointment, patient)
    assert derive_event_id(appointment, patient) != derive_event_id(appointment, doctor)


def test_event_ids_use_only_characters_google_accepts() -> None:
    """Google rejects an id outside base32hex with a 400 that says only "Invalid resource id".

    Cheap to assert here; expensive to diagnose in production.
    """
    import uuid

    permitted = set("0123456789abcdefghijklmnopqrstuv")
    for _ in range(50):
        event_id = derive_event_id(uuid.uuid4(), uuid.uuid4())
        assert set(event_id) <= permitted
        assert 5 <= len(event_id) <= 1024


def test_event_ids_do_not_leak_the_appointment_id() -> None:
    """The id is visible in a user's calendar export; it must not be an API identifier."""
    import uuid

    appointment = uuid.uuid4()
    event_id = derive_event_id(appointment, uuid.uuid4())
    assert appointment.hex not in event_id
    assert str(appointment) not in event_id


# --------------------------------------------------------------------------- writing


async def test_creating_an_event_posts_it_with_our_own_id() -> None:
    gateway, seen = build_gateway(lambda request: httpx.Response(200, json={"id": "abc12"}))

    await gateway.upsert_event(
        refresh_token="refresh", calendar_id="primary", event=make_event(), exists=False
    )

    request = calendar_requests(seen)[0]
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer fresh-access-token"

    import json

    body = json.loads(request.content)
    assert body["id"] == "abc12"
    assert body["start"]["dateTime"].startswith("2026-09-01T09:00")


async def test_a_create_that_already_happened_updates_instead_of_duplicating() -> None:
    """The idempotency guarantee: a retry after a lost response must not make a second event."""
    responses = iter([httpx.Response(409, json={}), httpx.Response(200, json={})])
    gateway, seen = build_gateway(lambda request: next(responses))

    await gateway.upsert_event(
        refresh_token="refresh", calendar_id="primary", event=make_event(), exists=False
    )

    methods = [request.method for request in calendar_requests(seen)]
    assert methods == ["POST", "PUT"]


async def test_an_event_deleted_by_the_user_is_recreated_on_update() -> None:
    """The reverse repair: we think it exists, Google says it does not."""
    responses = iter([httpx.Response(404, json={}), httpx.Response(200, json={})])
    gateway, seen = build_gateway(lambda request: next(responses))

    await gateway.upsert_event(
        refresh_token="refresh", calendar_id="primary", event=make_event(), exists=True
    )

    methods = [request.method for request in calendar_requests(seen)]
    assert methods == ["PUT", "POST"]


async def test_an_existing_event_is_updated_in_one_request() -> None:
    gateway, seen = build_gateway(lambda request: httpx.Response(200, json={}))

    await gateway.upsert_event(
        refresh_token="refresh", calendar_id="primary", event=make_event(), exists=True
    )

    requests = calendar_requests(seen)
    assert len(requests) == 1
    assert requests[0].method == "PUT"


async def test_the_calendar_id_is_url_encoded() -> None:
    """Calendar ids are often email addresses; an unencoded `@` would address the wrong path."""
    gateway, seen = build_gateway(lambda request: httpx.Response(200, json={}))

    await gateway.upsert_event(
        refresh_token="refresh",
        calendar_id="clinic@example.com",
        event=make_event(),
        exists=True,
    )

    assert "clinic%40example.com" in str(calendar_requests(seen)[0].url)


# --------------------------------------------------------------------------- deleting


async def test_deleting_an_event_that_is_already_gone_succeeds() -> None:
    """Cancelling an appointment whose event the user deleted by hand must not dead-letter."""
    gateway, _ = build_gateway(lambda request: httpx.Response(404, json={}))

    await gateway.delete_event(refresh_token="refresh", calendar_id="primary", event_id="abc12")


async def test_deleting_uses_the_delete_verb() -> None:
    gateway, seen = build_gateway(lambda request: httpx.Response(204))

    await gateway.delete_event(refresh_token="refresh", calendar_id="primary", event_id="abc12")

    request = calendar_requests(seen)[0]
    assert request.method == "DELETE"
    assert request.url.path.endswith("/events/abc12")


# --------------------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (429, {}),
        (500, {}),
        (503, {}),
        (403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}),
        (403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}),
    ],
)
async def test_temporary_problems_are_retryable(status_code: int, payload: dict[str, Any]) -> None:
    gateway, _ = build_gateway(lambda request: httpx.Response(status_code, json=payload))

    with pytest.raises(CalendarTransientError):
        await gateway.upsert_event(
            refresh_token="refresh", calendar_id="primary", event=make_event(), exists=True
        )


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (400, {"error": {"errors": [{"reason": "invalid"}]}}),
        (403, {"error": {"errors": [{"reason": "forbidden"}]}}),
        (404, {"error": {"errors": [{"reason": "notFound"}]}}),
    ],
)
async def test_permanent_rejections_are_not_retried(
    status_code: int, payload: dict[str, Any]
) -> None:
    """A 403 for a rate limit and a 403 for "you may not do this" must not be conflated."""
    responses = iter([httpx.Response(status_code, json=payload)] * 2)
    gateway, _ = build_gateway(lambda request: next(responses))

    with pytest.raises(CalendarPermanentError):
        await gateway.upsert_event(
            refresh_token="refresh", calendar_id="primary", event=make_event(), exists=True
        )


async def test_a_401_means_the_grant_is_gone_not_that_the_token_expired() -> None:
    """The access token was minted moments earlier, so 401 can only mean revocation."""
    gateway, _ = build_gateway(lambda request: httpx.Response(401, json={}))

    with pytest.raises(GoogleAuthRevoked):
        await gateway.upsert_event(
            refresh_token="refresh", calendar_id="primary", event=make_event(), exists=True
        )


async def test_a_network_failure_is_retryable() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "fresh-access-token", "expires_in": 3600}
            )
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(explode)
    oauth = GoogleOAuthClient(
        client_id="c", client_secret="s", redirect_uri="http://x", transport=transport
    )
    gateway = GoogleCalendarGateway(oauth, transport=transport)

    with pytest.raises(CalendarTransientError, match="could not reach"):
        await gateway.delete_event(refresh_token="refresh", calendar_id="primary", event_id="abc12")


async def test_the_error_message_never_carries_the_access_token() -> None:
    """`last_error` is stored and logged; a token in it would be a credential leak."""
    gateway, _ = build_gateway(
        lambda request: httpx.Response(400, json={"error": {"message": "bad request"}})
    )

    with pytest.raises(CalendarPermanentError) as caught:
        await gateway.upsert_event(
            refresh_token="refresh", calendar_id="primary", event=make_event(), exists=True
        )
    assert "fresh-access-token" not in str(caught.value)
    assert "refresh" not in str(caught.value)


# --------------------------------------------------------------------------- token caching


async def test_one_access_token_is_reused_across_calls() -> None:
    """Twenty events in a worker pass must not mean twenty token refreshes."""
    gateway, seen = build_gateway(lambda request: httpx.Response(200, json={}))

    for index in range(3):
        await gateway.upsert_event(
            refresh_token="refresh",
            calendar_id="primary",
            event=make_event(f"event{index}"),
            exists=True,
        )

    refreshes = [request for request in seen if request.url.host == "oauth2.googleapis.com"]
    assert len(refreshes) == 1


# --------------------------------------------------------------------------- consent URL


def test_the_consent_url_asks_for_offline_access_and_forces_consent() -> None:
    """Both parameters are required for a refresh token; without one the connection is
    unrenewable and stops working an hour later."""
    url = authorization_url(
        client_id="client-id", redirect_uri="http://testserver/cb", state="signed-state"
    )

    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=signed-state" in url
    assert "calendar.events" in url


def test_the_consent_url_requests_no_more_than_event_access() -> None:
    """A scope creep to full `calendar` would grant deletion of the user's whole calendar."""
    url = authorization_url(client_id="c", redirect_uri="http://x", state="s")

    assert "auth%2Fcalendar.events" in url
    assert "auth%2Fcalendar+" not in url
    assert "calendar.readonly" not in url
