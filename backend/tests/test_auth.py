"""Registration, login and identity behaviour."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserRole

MakeUser = Callable[..., Awaitable[User]]


async def test_registration_creates_a_patient(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register",
        json={
            "email": "meera@example.com",
            "password": "a-long-enough-password",
            "full_name": "Meera Nair",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "meera@example.com"
    assert body["role"] == "patient"
    assert body["is_active"] is True


async def test_registration_never_returns_the_password_or_its_hash(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register",
        json={
            "email": "leak@example.com",
            "password": "a-long-enough-password",
            "full_name": "Leak Check",
        },
    )

    serialised = response.text
    assert "a-long-enough-password" not in serialised
    assert "password_hash" not in serialised
    assert "$argon2" not in serialised


async def test_registration_ignores_a_role_supplied_by_the_caller(client: AsyncClient) -> None:
    """Security: the role is fixed server-side. Honouring it would let anyone self-promote."""
    response = await client.post(
        "/auth/register",
        json={
            "email": "sneaky@example.com",
            "password": "a-long-enough-password",
            "full_name": "Sneaky Person",
            "role": "admin",
        },
    )

    assert response.status_code == 201
    assert response.json()["role"] == "patient"


async def test_duplicate_email_is_rejected(client: AsyncClient) -> None:
    payload = {
        "email": "twice@example.com",
        "password": "a-long-enough-password",
        "full_name": "First Person",
    }
    first = await client.post("/auth/register", json=payload)
    assert first.status_code == 201

    second = await client.post("/auth/register", json=payload)
    assert second.status_code == 409


async def test_email_uniqueness_ignores_capitalisation(client: AsyncClient) -> None:
    await client.post(
        "/auth/register",
        json={
            "email": "Casing@Example.com",
            "password": "a-long-enough-password",
            "full_name": "Original",
        },
    )
    duplicate = await client.post(
        "/auth/register",
        json={
            "email": "casing@example.com",
            "password": "a-long-enough-password",
            "full_name": "Impostor",
        },
    )

    assert duplicate.status_code == 409


async def test_short_password_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register",
        json={"email": "short@example.com", "password": "abc", "full_name": "Short Pass"},
    )

    assert response.status_code == 422


async def test_login_returns_a_token_that_identifies_the_user(
    client: AsyncClient, make_user: MakeUser, default_password: str
) -> None:
    user = await make_user(email="login@example.com", full_name="Login Person")

    login = await client.post(
        "/auth/login",
        json={"email": "login@example.com", "password": default_password},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["id"] == str(user.id)
    assert me.json()["full_name"] == "Login Person"


async def test_login_accepts_a_differently_capitalised_email(
    client: AsyncClient, make_user: MakeUser, default_password: str
) -> None:
    await make_user(email="mixed@example.com")

    login = await client.post(
        "/auth/login",
        json={"email": "MIXED@Example.COM", "password": default_password},
    )

    assert login.status_code == 200


async def test_wrong_password_and_unknown_email_are_indistinguishable(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Different responses would let an attacker discover who is a patient of this clinic."""
    await make_user(email="known@example.com")

    wrong_password = await client.post(
        "/auth/login",
        json={"email": "known@example.com", "password": "not-the-right-password"},
    )
    unknown_email = await client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "not-the-right-password"},
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


async def test_deactivated_account_cannot_log_in(
    client: AsyncClient, make_user: MakeUser, default_password: str
) -> None:
    await make_user(email="gone@example.com", is_active=False)

    response = await client.post(
        "/auth/login",
        json={"email": "gone@example.com", "password": default_password},
    )

    assert response.status_code == 403


async def test_deactivating_a_user_invalidates_an_issued_token(
    client: AsyncClient,
    make_user: MakeUser,
    auth_header: Callable[[User], dict[str, str]],
    db_session: AsyncSession,
) -> None:
    """A token already in the wild must stop working once the account is disabled."""
    user = await make_user(email="revoked@example.com")
    headers = auth_header(user)

    assert (await client.get("/auth/me", headers=headers)).status_code == 200

    user.is_active = False
    await db_session.commit()

    assert (await client.get("/auth/me", headers=headers)).status_code == 401


async def test_me_requires_a_token(client: AsyncClient) -> None:
    response = await client.get("/auth/me")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_me_rejects_a_garbage_token(client: AsyncClient) -> None:
    response = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401


async def test_me_rejects_a_token_for_a_deleted_user(
    client: AsyncClient,
    make_user: MakeUser,
    auth_header: Callable[[User], dict[str, str]],
    db_session: AsyncSession,
) -> None:
    user = await make_user(email="deleted@example.com", role=UserRole.PATIENT)
    headers = auth_header(user)

    await db_session.delete(user)
    await db_session.commit()

    assert (await client.get("/auth/me", headers=headers)).status_code == 401
