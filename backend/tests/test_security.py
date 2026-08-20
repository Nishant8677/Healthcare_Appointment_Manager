"""Password hashing and access-token behaviour, tested without a database or HTTP."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import (
    TOKEN_TYPE,
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.models import UserRole

SECRET = "unit-test-secret-that-is-comfortably-long-enough"
ALGORITHM = "HS256"


def _token(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "role": UserRole.PATIENT,
        "secret": SECRET,
        "algorithm": ALGORITHM,
        "expires_in_minutes": 60,
    }
    kwargs.update(overrides)
    return create_access_token(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------- passwords


def test_hashing_the_same_password_twice_gives_different_hashes() -> None:
    """Each hash carries its own salt; identical hashes would leak that two users share a
    password."""
    assert hash_password("same-password") != hash_password("same-password")


def test_correct_password_verifies() -> None:
    assert verify_password("right-password", hash_password("right-password"))


def test_wrong_password_does_not_verify() -> None:
    assert not verify_password("wrong-password", hash_password("right-password"))


def test_malformed_stored_hash_fails_instead_of_raising() -> None:
    """A corrupt row must be a failed login, not a 500 on the login route."""
    assert not verify_password("any-password", "not-a-valid-argon2-hash")


# ---------------------------------------------------------------- tokens


def test_token_round_trip_preserves_identity_and_role() -> None:
    user_id = uuid.uuid4()

    claims = decode_access_token(
        _token(user_id=user_id, role=UserRole.DOCTOR), secret=SECRET, algorithm=ALGORITHM
    )

    assert claims["sub"] == str(user_id)
    assert claims["role"] == UserRole.DOCTOR.value
    assert claims["type"] == TOKEN_TYPE


def test_each_token_has_a_unique_id() -> None:
    """`jti` makes single-token revocation possible later without invalidating all of them."""
    first = decode_access_token(_token(), secret=SECRET, algorithm=ALGORITHM)
    second = decode_access_token(_token(), secret=SECRET, algorithm=ALGORITHM)

    assert first["jti"] != second["jti"]


def test_expired_token_is_rejected() -> None:
    expired = _token(now=datetime.now(UTC) - timedelta(hours=2), expires_in_minutes=60)

    with pytest.raises(TokenError):
        decode_access_token(expired, secret=SECRET, algorithm=ALGORITHM)


def test_token_signed_with_another_secret_is_rejected() -> None:
    with pytest.raises(TokenError):
        decode_access_token(
            _token(),
            secret="a-completely-different-secret-of-adequate-length",
            algorithm=ALGORITHM,
        )


def test_tampered_token_is_rejected() -> None:
    header, payload, signature = _token().split(".")
    tampered = f"{header}.{payload[:-4]}AAAA.{signature}"

    with pytest.raises(TokenError):
        decode_access_token(tampered, secret=SECRET, algorithm=ALGORITHM)


def test_token_of_the_wrong_type_is_rejected() -> None:
    """Guards against a future refresh token being replayed as an access token."""
    import jwt

    not_an_access_token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": UserRole.PATIENT.value,
            "type": "refresh",
            "exp": datetime.now(UTC) + timedelta(minutes=30),
        },
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(TokenError):
        decode_access_token(not_an_access_token, secret=SECRET, algorithm=ALGORITHM)


def test_token_missing_required_claims_is_rejected() -> None:
    import jwt

    incomplete = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) + timedelta(minutes=30)},
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(TokenError):
        decode_access_token(incomplete, secret=SECRET, algorithm=ALGORITHM)
