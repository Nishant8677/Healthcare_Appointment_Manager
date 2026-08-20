"""Connecting and disconnecting a Google Calendar.

The awkward part of an OAuth callback in a token-authenticated API is that Google redirects
the *browser* back, so the request arrives with no `Authorization` header — the endpoint has
no idea who it is for. The usual fix is a server-side table of pending `state` values; this
uses a short-lived signed token instead, which carries the user id in a form that cannot be
forged and needs no storage, no cleanup job, and no row that outlives its purpose.

That signature is doing real work. Without it, anyone could call the callback with their own
Google authorisation code and a guessed state, and attach their calendar to somebody else's
clinic account — or worse, attach a calendar they control to a doctor's account and receive a
copy of every consultation on that doctor's schedule.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings, get_current_user, get_session, require_roles
from app.core.config import Settings
from app.core.security import (
    TokenError,
    create_calendar_state_token,
    decode_calendar_state_token,
)
from app.models.calendar import CalendarConnection
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.calendar import (
    CalendarAuthorizationResponse,
    CalendarCallbackResponse,
    CalendarConnectionResponse,
)
from app.services import calendar_sync
from app.services.google_calendar import build_oauth_client
from app.services.google_oauth import GoogleAuthError, GoogleAuthRevoked, authorization_url
from app.services.token_crypto import TokenCipher, TokenEncryptionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["calendar"])

_NOT_CONFIGURED = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail=(
        "Google Calendar is not configured on this deployment. "
        "Everything else works; calendar events are simply not created."
    ),
)


def _require_cipher(settings: Settings) -> TokenCipher:
    if settings.calendar_token_key is None:
        raise _NOT_CONFIGURED
    try:
        return TokenCipher(settings.calendar_token_key.get_secret_value())
    except TokenEncryptionError as exc:
        # A malformed key is a deployment error, not a user error, and must not read as one.
        logger.error("calendar token key is unusable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Calendar encryption is misconfigured on this deployment.",
        ) from exc


def _connection_response(connection: CalendarConnection | None) -> CalendarConnectionResponse:
    if connection is None:
        return CalendarConnectionResponse(connected=False)
    return CalendarConnectionResponse(
        connected=connection.is_active,
        google_account_email=connection.google_account_email,
        calendar_id=connection.calendar_id,
        connected_at=connection.created_at,
        revoked_at=connection.revoked_at,
        last_error=connection.last_error,
    )


@router.post(
    "/connect",
    response_model=CalendarAuthorizationResponse,
    summary="Start connecting a Google Calendar",
)
async def start_connect(
    user: User = Depends(require_roles(UserRole.PATIENT, UserRole.DOCTOR)),
    settings: Settings = Depends(get_app_settings),
) -> CalendarAuthorizationResponse:
    """Return the Google consent URL for the signed-in user.

    Restricted to patients and doctors because they are the two parties an appointment puts
    on a calendar; an admin has no appointments of their own to sync.
    """
    if settings.google_client_id is None:
        raise _NOT_CONFIGURED

    state = create_calendar_state_token(
        user_id=user.id,
        secret=settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=settings.calendar_state_ttl_minutes,
    )
    return CalendarAuthorizationResponse(
        authorization_url=authorization_url(
            client_id=settings.google_client_id,
            redirect_uri=settings.google_redirect_uri,
            state=state,
        ),
        expires_in_minutes=settings.calendar_state_ttl_minutes,
    )


@router.get(
    "/callback",
    summary="Google OAuth redirect target",
    response_model=None,
    responses={200: {"model": CalendarCallbackResponse}},
)
async def oauth_callback(
    state: str = Query(description="The signed state issued by /calendar/connect."),
    code: str | None = Query(default=None, description="Google's authorisation code."),
    error: str | None = Query(default=None, description="Set when the user declined."),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> CalendarCallbackResponse | RedirectResponse:
    """Exchange the authorisation code and store the connection.

    Unauthenticated by design — the browser arrives here without a bearer token — but not
    unauthorised: the `state` is a signature over the user id, so this endpoint can only ever
    act on behalf of whoever `/calendar/connect` issued it to.
    """
    oauth = build_oauth_client(settings)
    if oauth is None:
        raise _NOT_CONFIGURED
    cipher = _require_cipher(settings)

    try:
        user_id = decode_calendar_state_token(
            state,
            secret=settings.jwt_secret.get_secret_value(),
            algorithm=settings.jwt_algorithm,
        )
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This authorisation link is invalid or has expired. Start again.",
        ) from exc

    if error is not None:
        # The user pressed "cancel" on Google's consent screen. A normal outcome, reported
        # as such rather than as a server error.
        return _finish(settings, connected=False, reason=error)
    if code is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return an authorisation code.",
        )

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The account this authorisation was started for is no longer active.",
        )

    try:
        grant = await oauth.exchange_code(code)
    except (GoogleAuthError, GoogleAuthRevoked) as exc:
        logger.warning("calendar authorisation exchange failed", extra={"user_id": str(user_id)})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google did not complete the authorisation: {exc}",
        ) from exc

    connection = await calendar_sync.get_connection(session, user_id)
    if connection is None:
        connection = CalendarConnection(user_id=user_id, calendar_id="primary")
        session.add(connection)

    connection.google_account_email = grant.account_email
    connection.encrypted_refresh_token = cipher.encrypt(grant.refresh_token)
    connection.granted_scope = grant.scope
    # Reconnecting clears a previous revocation: this is a fresh, working grant.
    connection.revoked_at = None
    connection.last_error = None

    # Flush before backfilling so the connection is visible to the queries that follow.
    await session.flush()
    queued = await calendar_sync.backfill_for_user(
        session,
        user=user,
        connection=connection,
        zone=settings.clinic_zone,
        limit=settings.calendar_backfill_limit,
    )
    await session.commit()

    logger.info(
        "calendar connected",
        extra={"user_id": str(user_id), "appointments_queued": queued},
    )
    return _finish(
        settings,
        connected=True,
        account_email=grant.account_email,
        queued=queued,
    )


@router.get(
    "/connection",
    response_model=CalendarConnectionResponse,
    summary="Whether this user's calendar is connected",
)
async def read_connection(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CalendarConnectionResponse:
    return _connection_response(await calendar_sync.get_connection(session, user.id))


@router.delete(
    "/connection",
    response_model=CalendarConnectionResponse,
    summary="Disconnect this user's calendar",
)
async def disconnect(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> CalendarConnectionResponse:
    """Withdraw the grant at Google and delete the stored token.

    The row is removed rather than flagged: the user asked for the credential to be gone, and
    keeping an encrypted refresh token they believe they deleted is not defensible. Pending
    sync rows are left alone — the worker will find no connection and mark them skipped,
    which is an accurate record of what happened.
    """
    connection = await calendar_sync.get_connection(session, user.id)
    if connection is None:
        return CalendarConnectionResponse(connected=False)

    oauth = build_oauth_client(settings)
    if oauth is not None and settings.calendar_token_key is not None:
        try:
            cipher = TokenCipher(settings.calendar_token_key.get_secret_value())
            await oauth.revoke(cipher.decrypt(connection.encrypted_refresh_token))
        except TokenEncryptionError:
            # An unreadable token cannot be revoked at Google, but it also cannot be used by
            # anyone, and deleting the row is still the right outcome.
            logger.warning(
                "disconnecting a calendar whose token could not be decrypted",
                extra={"user_id": str(user.id)},
            )

    await session.delete(connection)
    await session.commit()
    logger.info("calendar disconnected", extra={"user_id": str(user.id)})
    return CalendarConnectionResponse(connected=False, revoked_at=datetime.now(UTC))


def _finish(
    settings: Settings,
    *,
    connected: bool,
    account_email: str = "",
    queued: int = 0,
    reason: str | None = None,
) -> CalendarCallbackResponse | RedirectResponse:
    """Hand the browser back to the frontend, or answer with JSON if there isn't one.

    The redirect target comes from configuration, never from the request, so this cannot be
    turned into an open redirect by adding a parameter to the callback URL.
    """
    if settings.calendar_return_url:
        outcome = "connected" if connected else "declined"
        separator = "&" if "?" in settings.calendar_return_url else "?"
        return RedirectResponse(
            url=f"{settings.calendar_return_url}{separator}calendar={outcome}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not connected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Calendar access was not granted ({reason or 'declined'}).",
        )
    return CalendarCallbackResponse(
        connected=True, google_account_email=account_email, appointments_queued=queued
    )
