"""Password hashing and access-token issuing.

Pure functions over explicit arguments: no module-level configuration, so the token helpers
can be exercised in tests without building an application.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.models.enums import UserRole

# Argon2id at the library's defaults, which follow current OWASP guidance. Chosen over
# bcrypt for its memory-hardness and because it has no silent 72-byte password truncation.
_hasher = PasswordHasher()

# The `type` claim, not a credential: it distinguishes access tokens from any future
# refresh token so one cannot be replayed as the other.
TOKEN_TYPE = "access"  # noqa: S105


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or of the wrong type."""


def hash_password(plain_password: str) -> str:
    """Return an Argon2 hash, salt included, safe to store verbatim."""
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Check a password against a stored hash without raising on mismatch.

    A stored hash that is malformed (truncated column, hand-edited row) is treated as a
    failed verification rather than an error, so a corrupt row cannot become a 500 on the
    login path.
    """
    try:
        return _hasher.verify(password_hash, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with weaker parameters than the current settings."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def create_access_token(
    *,
    user_id: uuid.UUID,
    role: UserRole,
    secret: str,
    algorithm: str,
    expires_in_minutes: int,
    now: datetime | None = None,
) -> str:
    """Issue a signed access token.

    `now` is injectable so expiry behaviour can be tested without waiting or patching the
    clock globally.
    """
    issued_at = now or datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role.value,
        "type": TOKEN_TYPE,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=expires_in_minutes),
        # A unique id per token, so individual tokens can be revoked later without
        # invalidating every token the user holds.
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_access_token(token: str, *, secret: str, algorithm: str) -> dict[str, Any]:
    """Verify a token and return its claims.

    Raises:
        TokenError: if the signature, expiry, structure or token type is not valid.
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            options={"require": ["exp", "sub", "role", "type"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    # Guards against a future refresh token being replayed as an access token.
    if claims.get("type") != TOKEN_TYPE:
        raise TokenError("token is not an access token")

    return claims
