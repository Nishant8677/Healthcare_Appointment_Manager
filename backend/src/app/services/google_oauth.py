"""The Google OAuth 2.0 authorization-code flow, spoken directly over HTTPS.

No `google-auth-oauthlib` or `google-api-python-client`. Those pull in roughly ten transitive
packages — protobuf, grpc, `google-api-core` — to wrap what is, for our purposes, four HTTP
requests: send the user to a consent screen, trade the code for tokens, refresh an access
token, revoke a grant. The whole flow is below in about a hundred lines, using the `httpx`
already in the project, and every request is visible rather than buried in a generated client.

The security-relevant choices are `access_type=offline` (without it Google returns no refresh
token, so the clinic could only write to a calendar while the user was actively browsing) and
the narrowest useful scope: `calendar.events` grants event read/write and nothing else — not
calendar creation, not sharing settings, not the user's other calendars' metadata.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105
REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

# `openid`/`email` identify which Google account granted access, so the portal can show the
# user which calendar they connected. `calendar.events` is the write scope, and is
# deliberately not the broader `calendar`.
SCOPES: tuple[str, ...] = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
)

# Refresh a little before the hour is up: a token that expires mid-request would otherwise
# produce a spurious 401 and a pointless retry.
_EXPIRY_SAFETY_MARGIN = timedelta(seconds=60)


class GoogleAuthError(Exception):
    """The authorisation flow failed in a way that is worth retrying or reporting."""


class GoogleAuthRevoked(Exception):
    """The user's grant is gone — revoked in their Google account, or expired.

    Distinct from `GoogleAuthError` because it is terminal: no amount of retrying brings back
    a grant the user has withdrawn. The connection is marked revoked and the user is asked to
    reconnect.
    """


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    """What the consent screen produced."""

    refresh_token: str
    access_token: str
    expires_at: datetime
    account_email: str
    scope: str


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    expires_at: datetime

    def is_valid_at(self, moment: datetime) -> bool:
        return moment + _EXPIRY_SAFETY_MARGIN < self.expires_at


def authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Build the URL the user is sent to in order to grant access.

    `prompt=consent` is not redundant with `access_type=offline`: Google issues a refresh
    token only on the *first* consent for a given client and account, so a user who
    disconnects and reconnects would otherwise come back with an access token and no way to
    renew it. Forcing the consent screen each time guarantees a refresh token every time.
    """
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{AUTHORIZATION_ENDPOINT}?{query}"


class GoogleOAuthClient:
    """Token exchange, refresh and revocation."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._timeout = timeout_seconds
        # Injectable so the exact request shape can be asserted against a mock transport,
        # without a network or a real Google project.
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    async def exchange_code(self, code: str) -> OAuthGrant:
        """Trade an authorisation code for tokens, and find out whose account it is."""
        payload = await self._post_token(
            {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
            }
        )

        refresh_token = _string_field(payload, "refresh_token")
        access_token = _string_field(payload, "access_token")
        if not refresh_token or not access_token:
            # Reached when a user has an existing grant and Google withholds the refresh
            # token. `prompt=consent` above is what prevents it; failing loudly here means a
            # regression in that parameter cannot silently produce unrenewable connections.
            raise GoogleAuthError(
                "Google returned no refresh token. Remove this app from your Google account's "
                "third-party access list and connect again."
            )

        email = await self.account_email(access_token)
        return OAuthGrant(
            refresh_token=refresh_token,
            access_token=access_token,
            expires_at=_expiry_from(payload),
            account_email=email,
            scope=_string_field(payload, "scope") or "",
        )

    async def refresh_access_token(self, refresh_token: str) -> AccessToken:
        """Mint a fresh access token from a stored refresh token."""
        payload = await self._post_token(
            {
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
            }
        )
        access_token = _string_field(payload, "access_token")
        if not access_token:
            raise GoogleAuthError("Google returned no access token for the refresh request.")
        return AccessToken(token=access_token, expires_at=_expiry_from(payload))

    async def account_email(self, access_token: str) -> str:
        """Which Google account this token belongs to.

        Read from the userinfo endpoint rather than by decoding the `id_token`. Decoding a
        JWT without verifying it is defensible here — it came straight from Google's token
        endpoint over TLS — but "we skip signature verification, and here is why it's fine"
        is a sentence worth not having in a healthcare codebase. One extra request, made once
        per connection, buys its absence.
        """
        try:
            async with self._http() as client:
                response = await client.get(
                    USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
                )
        except httpx.HTTPError as exc:
            raise GoogleAuthError(f"could not reach Google's userinfo endpoint: {exc}") from exc

        if response.status_code >= 400:
            raise GoogleAuthError(f"Google rejected the userinfo request ({response.status_code}).")
        email = response.json().get("email")
        if not email:
            raise GoogleAuthError("Google did not return an email address for this account.")
        return str(email)

    async def revoke(self, token: str) -> None:
        """Withdraw a grant at Google.

        Failures are logged and swallowed: the caller is disconnecting, and the local record
        must be removed either way. Leaving a row behind because Google was briefly
        unreachable would mean the user cannot disconnect during an outage.
        """
        try:
            async with self._http() as client:
                response = await client.post(REVOCATION_ENDPOINT, data={"token": token})
        except httpx.HTTPError as exc:
            logger.warning("could not reach Google to revoke a token", extra={"error": str(exc)})
            return
        if response.status_code >= 400:
            logger.warning(
                "Google declined to revoke a token",
                extra={"status_code": response.status_code},
            )

    async def _post_token(self, form: dict[str, str]) -> dict[str, object]:
        try:
            async with self._http() as client:
                response = await client.post(TOKEN_ENDPOINT, data=form)
        except httpx.HTTPError as exc:
            raise GoogleAuthError(f"could not reach Google's token endpoint: {exc}") from exc

        if response.status_code >= 400:
            detail = _error_code(response)
            # `invalid_grant` is Google's answer for a refresh token that has been revoked,
            # expired, or belongs to a deleted account. Retrying it forever would be pure
            # waste, so it is separated from every other failure here.
            if detail == "invalid_grant":
                raise GoogleAuthRevoked(
                    "Google reports this authorisation is no longer valid. "
                    "The calendar needs to be reconnected."
                )
            raise GoogleAuthError(f"Google returned {response.status_code} ({detail}).")

        body: dict[str, object] = response.json()
        return body


def _string_field(payload: dict[str, object], key: str) -> str | None:
    """A string field, or `None` if it is absent or not a string.

    A non-string where a token is expected is treated as missing rather than coerced: a
    `str()` of whatever Google sent would produce a token-shaped value that fails much
    later, at the first API call, instead of here.
    """
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _expiry_from(payload: dict[str, object]) -> datetime:
    raw = payload.get("expires_in", 3600)
    seconds = int(raw) if isinstance(raw, int | str) else 3600
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _error_code(response: httpx.Response) -> str:
    """Google's machine-readable error slug, never its full body.

    The body of a token-endpoint error can echo request parameters, and this string ends up
    in a stored `last_error` and in logs. Only the slug is taken.
    """
    try:
        payload = response.json()
    except ValueError:
        return "unparseable error response"
    if isinstance(payload, dict):
        code = payload.get("error")
        if isinstance(code, str):
            return code
    return "unknown error"
