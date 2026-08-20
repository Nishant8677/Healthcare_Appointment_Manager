"""Connecting a calendar over HTTP, and the admin view of the sync queue.

The security question this file exists to answer: the OAuth callback has no bearer token, so
what stops somebody calling it and attaching a calendar they control to another person's
clinic account? The answer is the signed `state`, and several tests below are attempts to
defeat it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    create_calendar_state_token,
)
from app.main import create_app
from app.models import DoctorProfile, User, UserRole
from app.models.appointment import Appointment
from app.models.calendar import CalendarConnection, CalendarSyncJob
from app.models.enums import AppointmentStatus, CalendarSyncAction, CalendarSyncStatus
from app.services.google_oauth import GoogleOAuthClient

Headers = dict[str, str]
MakeUser = Callable[..., Awaitable[User]]
MakeDoctor = Callable[..., Awaitable[DoctorProfile]]
ConnectCalendar = Callable[..., Awaitable[CalendarConnection]]

GOOGLE_ACCOUNT = "someone@gmail.example.com"


@pytest.fixture
def calendar_app(calendar_settings: Settings) -> FastAPI:
    return create_app(calendar_settings)


@pytest_asyncio.fixture
async def calendar_client(calendar_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with calendar_app.router.lifespan_context(calendar_app):
        transport = ASGITransport(app=calendar_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client


def google_answers(
    *, refresh_token: str | None = "1//refresh", email: str = GOOGLE_ACCOUNT
) -> httpx.MockTransport:
    """A stand-in for Google's token and userinfo endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body: dict[str, object] = {"access_token": "access", "expires_in": 3600, "scope": "s"}
            if refresh_token is not None:
                body["refresh_token"] = refresh_token
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"email": email})

    return httpx.MockTransport(handler)


def patch_google(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, transport: httpx.MockTransport
) -> None:
    """Point the callback's OAuth client at a mock transport instead of Google."""
    import app.api.calendar as calendar_api

    def build(_: Settings) -> GoogleOAuthClient:
        assert settings.google_client_id is not None
        assert settings.google_client_secret is not None
        return GoogleOAuthClient(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret.get_secret_value(),
            redirect_uri=settings.google_redirect_uri,
            transport=transport,
        )

    monkeypatch.setattr(calendar_api, "build_oauth_client", build)


def state_for(user: User, settings: Settings, *, minutes: int = 10) -> str:
    return create_calendar_state_token(
        user_id=user.id,
        secret=settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=minutes,
    )


# --------------------------------------------------------------------------- starting


async def test_connect_is_unavailable_when_google_is_not_configured(
    client: AsyncClient, patient_headers: Headers
) -> None:
    """The default deployment. It must say so plainly rather than 500."""
    response = await client.post("/calendar/connect", headers=patient_headers)

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


async def test_connect_returns_a_consent_url_carrying_a_signed_state(
    calendar_client: AsyncClient, patient_headers: Headers
) -> None:
    response = await calendar_client.post("/calendar/connect", headers=patient_headers)

    assert response.status_code == 200, response.text
    url = response.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/")
    assert "access_type=offline" in url
    assert "state=" in url


async def test_connect_requires_authentication(calendar_client: AsyncClient) -> None:
    assert (await calendar_client.post("/calendar/connect")).status_code == 401


async def test_an_admin_has_no_calendar_to_connect(
    calendar_client: AsyncClient, admin_headers: Headers
) -> None:
    assert (
        await calendar_client.post("/calendar/connect", headers=admin_headers)
    ).status_code == 403


# --------------------------------------------------------------------------- the callback


async def test_a_valid_callback_stores_an_encrypted_connection(
    calendar_client: AsyncClient,
    calendar_settings: Settings,
    make_user: MakeUser,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = await make_user()
    patch_google(monkeypatch, calendar_settings, google_answers())

    response = await calendar_client.get(
        "/calendar/callback",
        params={"code": "auth-code", "state": state_for(patient, calendar_settings)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["google_account_email"] == GOOGLE_ACCOUNT

    result = await db_session.execute(
        select(CalendarConnection).where(CalendarConnection.user_id == patient.id)
    )
    connection = result.scalar_one()
    assert connection.is_active
    # The stored value must not be the token itself.
    assert "1//refresh" not in connection.encrypted_refresh_token


async def test_the_callback_never_returns_a_token(
    calendar_client: AsyncClient,
    calendar_settings: Settings,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response is rendered into a browser URL bar and a server log."""
    patient = await make_user()
    patch_google(monkeypatch, calendar_settings, google_answers())

    response = await calendar_client.get(
        "/calendar/callback",
        params={"code": "auth-code", "state": state_for(patient, calendar_settings)},
    )

    assert "1//refresh" not in response.text
    assert "access" not in response.json()


async def test_a_forged_state_is_rejected(
    calendar_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, calendar_settings: Settings
) -> None:
    """Without the signature check, this request would attach an attacker's calendar to
    whichever account they named."""
    patch_google(monkeypatch, calendar_settings, google_answers())

    response = await calendar_client.get(
        "/calendar/callback", params={"code": "auth-code", "state": "not-a-real-token"}
    )

    assert response.status_code == 400
    assert "invalid or has expired" in response.json()["detail"]


async def test_an_access_token_cannot_be_used_as_a_state(
    calendar_client: AsyncClient,
    calendar_settings: Settings,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both are signed with the same key, so only the `type` claim separates them. A stolen
    bearer token pasted into a callback URL must not connect a calendar."""
    patient = await make_user()
    patch_google(monkeypatch, calendar_settings, google_answers())
    access_token = create_access_token(
        user_id=patient.id,
        role=patient.role,
        secret=calendar_settings.jwt_secret.get_secret_value(),
        algorithm=calendar_settings.jwt_algorithm,
        expires_in_minutes=60,
    )

    response = await calendar_client.get(
        "/calendar/callback", params={"code": "auth-code", "state": access_token}
    )

    assert response.status_code == 400


async def test_an_expired_state_is_rejected(
    calendar_client: AsyncClient,
    calendar_settings: Settings,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = await make_user()
    patch_google(monkeypatch, calendar_settings, google_answers())

    response = await calendar_client.get(
        "/calendar/callback",
        params={"code": "auth-code", "state": state_for(patient, calendar_settings, minutes=-1)},
    )

    assert response.status_code == 400


async def test_declining_consent_is_reported_not_crashed(
    calendar_client: AsyncClient,
    calendar_settings: Settings,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = await make_user()
    patch_google(monkeypatch, calendar_settings, google_answers())

    response = await calendar_client.get(
        "/calendar/callback",
        params={"error": "access_denied", "state": state_for(patient, calendar_settings)},
    )

    assert response.status_code == 400
    assert "access_denied" in response.json()["detail"]


async def test_a_grant_without_a_refresh_token_is_refused(
    calendar_client: AsyncClient,
    calendar_settings: Settings,
    make_user: MakeUser,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storing a connection that can only work for an hour would look like success and fail
    silently at the next poll."""
    patient = await make_user()
    patch_google(monkeypatch, calendar_settings, google_answers(refresh_token=None))

    response = await calendar_client.get(
        "/calendar/callback",
        params={"code": "auth-code", "state": state_for(patient, calendar_settings)},
    )

    assert response.status_code == 502
    result = await db_session.execute(select(CalendarConnection))
    assert result.scalars().all() == []


async def test_reconnecting_replaces_the_connection_rather_than_adding_one(
    calendar_client: AsyncClient,
    calendar_settings: Settings,
    make_user: MakeUser,
    connect_calendar: ConnectCalendar,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unique constraint on `user_id` means a second row is not merely untidy but
    impossible; reconnecting has to update in place, and clear an earlier revocation."""
    patient = await make_user()
    await connect_calendar(patient, revoked=True)
    patch_google(monkeypatch, calendar_settings, google_answers())

    response = await calendar_client.get(
        "/calendar/callback",
        params={"code": "auth-code", "state": state_for(patient, calendar_settings)},
    )

    assert response.status_code == 200, response.text
    result = await db_session.execute(
        select(CalendarConnection).where(CalendarConnection.user_id == patient.id)
    )
    connections = result.scalars().all()
    assert len(connections) == 1
    assert connections[0].revoked_at is None


async def test_connecting_queues_existing_upcoming_appointments(
    calendar_client: AsyncClient,
    calendar_settings: Settings,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the backfill, connecting appears to do nothing until the next booking, and
    the first person to try it concludes the feature is broken."""
    patient = await make_user()
    doctor = await make_doctor()
    starts = datetime.now(UTC) + timedelta(days=2)
    db_session.add(
        Appointment(
            patient_id=patient.id,
            doctor_profile_id=doctor.id,
            starts_at=starts,
            ends_at=starts + timedelta(minutes=30),
            status=AppointmentStatus.CONFIRMED,
        )
    )
    await db_session.commit()
    patch_google(monkeypatch, calendar_settings, google_answers())

    response = await calendar_client.get(
        "/calendar/callback",
        params={"code": "auth-code", "state": state_for(patient, calendar_settings)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["appointments_queued"] == 1
    result = await db_session.execute(select(CalendarSyncJob))
    job = result.scalar_one()
    assert job.user_id == patient.id
    assert job.action is CalendarSyncAction.SYNC


async def test_a_configured_return_url_redirects_instead_of_answering_json(
    calendar_settings: Settings,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = calendar_settings.model_copy(
        update={"calendar_return_url": "http://localhost:5173/settings"}
    )
    app = create_app(settings)
    patient = await make_user()
    patch_google(monkeypatch, settings, google_answers())

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            response = await http_client.get(
                "/calendar/callback",
                params={"code": "auth-code", "state": state_for(patient, settings)},
            )

    assert response.status_code == 303
    assert response.headers["location"] == "http://localhost:5173/settings?calendar=connected"


# --------------------------------------------------------------------------- reading state


async def test_connection_reports_not_connected_by_default(
    calendar_client: AsyncClient, patient_headers: Headers
) -> None:
    response = await calendar_client.get("/calendar/connection", headers=patient_headers)

    assert response.status_code == 200
    assert response.json()["connected"] is False


async def test_connection_reports_the_google_account_but_never_a_token(
    calendar_client: AsyncClient,
    make_user: MakeUser,
    connect_calendar: ConnectCalendar,
    auth_header: Callable[[User], Headers],
) -> None:
    patient = await make_user()
    connection = await connect_calendar(patient, refresh_token="1//super-secret")

    response = await calendar_client.get("/calendar/connection", headers=auth_header(patient))

    body = response.json()
    assert body["connected"] is True
    assert body["google_account_email"] == connection.google_account_email
    assert "1//super-secret" not in response.text
    assert "refresh_token" not in body


async def test_a_revoked_connection_reports_itself_as_disconnected(
    calendar_client: AsyncClient,
    make_user: MakeUser,
    connect_calendar: ConnectCalendar,
    auth_header: Callable[[User], Headers],
) -> None:
    """ "Connected but broken" and "never connected" need different words in the portal."""
    patient = await make_user()
    await connect_calendar(patient, revoked=True)

    body = (await calendar_client.get("/calendar/connection", headers=auth_header(patient))).json()

    assert body["connected"] is False
    assert body["revoked_at"] is not None


# --------------------------------------------------------------------------- disconnecting


async def test_disconnecting_deletes_the_stored_token(
    calendar_client: AsyncClient,
    make_user: MakeUser,
    connect_calendar: ConnectCalendar,
    auth_header: Callable[[User], Headers],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    calendar_settings: Settings,
) -> None:
    """A user who presses disconnect must not leave an encrypted refresh token behind."""
    patient = await make_user()
    await connect_calendar(patient)
    patch_google(monkeypatch, calendar_settings, google_answers())

    response = await calendar_client.delete("/calendar/connection", headers=auth_header(patient))

    assert response.status_code == 200
    assert response.json()["connected"] is False
    result = await db_session.execute(
        select(CalendarConnection).where(CalendarConnection.user_id == patient.id)
    )
    assert result.scalar_one_or_none() is None


async def test_disconnecting_when_never_connected_is_not_an_error(
    calendar_client: AsyncClient, patient_headers: Headers
) -> None:
    response = await calendar_client.delete("/calendar/connection", headers=patient_headers)

    assert response.status_code == 200
    assert response.json()["connected"] is False


# --------------------------------------------------------------------------- admin view


async def test_the_admin_sync_view_is_closed_to_patients(
    calendar_client: AsyncClient, patient_headers: Headers
) -> None:
    assert (
        await calendar_client.get("/admin/calendar/sync-jobs", headers=patient_headers)
    ).status_code == 403


async def test_the_admin_summary_counts_by_state(
    calendar_client: AsyncClient,
    admin_headers: Headers,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    db_session: AsyncSession,
) -> None:
    patient = await make_user()
    doctor = await make_doctor()
    starts = datetime.now(UTC) + timedelta(days=2)
    appointment = Appointment(
        patient_id=patient.id,
        doctor_profile_id=doctor.id,
        starts_at=starts,
        ends_at=starts + timedelta(minutes=30),
        status=AppointmentStatus.CONFIRMED,
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)

    for user, state in ((patient, CalendarSyncStatus.FAILED), (doctor.user_id, None)):
        db_session.add(
            CalendarSyncJob(
                appointment_id=appointment.id,
                user_id=user.id if isinstance(user, User) else user,
                action=CalendarSyncAction.SYNC,
                status=state or CalendarSyncStatus.PENDING,
                google_event_id=uuid.uuid4().hex[:20],
                calendar_id="primary",
                payload={
                    "summary": "s",
                    "description": "d",
                    "starts_at": starts.isoformat(),
                    "ends_at": starts.isoformat(),
                    "time_zone": "UTC",
                },
            )
        )
    await db_session.commit()

    body = (await calendar_client.get("/admin/calendar/summary", headers=admin_headers)).json()

    assert body == {"pending": 1, "synced": 0, "skipped": 0, "failed": 1}


async def test_a_skipped_entry_can_be_requeued_once_the_user_connects(
    calendar_client: AsyncClient,
    admin_headers: Headers,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    db_session: AsyncSession,
) -> None:
    """The common repair: the entry was skipped because there was no calendar, and now
    there is one."""
    patient = await make_user()
    doctor = await make_doctor()
    starts = datetime.now(UTC) + timedelta(days=2)
    appointment = Appointment(
        patient_id=patient.id,
        doctor_profile_id=doctor.id,
        starts_at=starts,
        ends_at=starts + timedelta(minutes=30),
        status=AppointmentStatus.CONFIRMED,
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)
    job = CalendarSyncJob(
        appointment_id=appointment.id,
        user_id=patient.id,
        action=CalendarSyncAction.SYNC,
        status=CalendarSyncStatus.SKIPPED,
        google_event_id=uuid.uuid4().hex[:20],
        calendar_id="primary",
        payload={
            "summary": "s",
            "description": "d",
            "starts_at": starts.isoformat(),
            "ends_at": starts.isoformat(),
            "time_zone": "UTC",
        },
        attempts=2,
        last_error="no connected calendar for this user",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    response = await calendar_client.post(
        f"/admin/calendar/sync-jobs/{job.id}/retry", headers=admin_headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == CalendarSyncStatus.PENDING.value
    assert body["attempts"] == 0
    assert body["last_error"] is None


async def test_retrying_an_entry_that_is_already_queued_is_refused(
    calendar_client: AsyncClient,
    admin_headers: Headers,
    make_user: MakeUser,
    make_doctor: MakeDoctor,
    db_session: AsyncSession,
) -> None:
    patient = await make_user()
    doctor = await make_doctor()
    starts = datetime.now(UTC) + timedelta(days=2)
    appointment = Appointment(
        patient_id=patient.id,
        doctor_profile_id=doctor.id,
        starts_at=starts,
        ends_at=starts + timedelta(minutes=30),
        status=AppointmentStatus.CONFIRMED,
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)
    job = CalendarSyncJob(
        appointment_id=appointment.id,
        user_id=patient.id,
        action=CalendarSyncAction.SYNC,
        status=CalendarSyncStatus.PENDING,
        google_event_id=uuid.uuid4().hex[:20],
        calendar_id="primary",
        payload={
            "summary": "s",
            "description": "d",
            "starts_at": starts.isoformat(),
            "ends_at": starts.isoformat(),
            "time_zone": "UTC",
        },
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    response = await calendar_client.post(
        f"/admin/calendar/sync-jobs/{job.id}/retry", headers=admin_headers
    )

    assert response.status_code == 409


async def test_retrying_an_unknown_entry_is_a_404(
    calendar_client: AsyncClient, admin_headers: Headers
) -> None:
    response = await calendar_client.post(
        f"/admin/calendar/sync-jobs/{uuid.uuid4()}/retry", headers=admin_headers
    )

    assert response.status_code == 404


async def test_the_role_of_a_doctor_is_allowed_to_connect(
    calendar_client: AsyncClient,
    make_doctor: MakeDoctor,
    db_session: AsyncSession,
    auth_header: Callable[[User], Headers],
) -> None:
    doctor = await make_doctor()
    user = await db_session.get(User, doctor.user_id)
    assert user is not None and user.role is UserRole.DOCTOR

    response = await calendar_client.post("/calendar/connect", headers=auth_header(user))

    assert response.status_code == 200, response.text
