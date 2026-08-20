"""Shared test fixtures.

Tests run against a real Postgres database (`healthcare_test`), never SQLite: the booking
logic this project is judged on depends on Postgres row locking and partial unique indexes,
so a substitute engine would let broken concurrency code pass.

Isolation strategy: migrations are applied once per session, and every test starts from
truncated tables. Truncating *before* each test rather than after means a test that fails
midway cannot poison the next one.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.core.eventloop import configure_event_loop_policy
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.models import Base, DoctorProfile, User, UserRole

# Must run before pytest-asyncio creates any event loop: on Windows the default loop cannot
# run the async database driver.
configure_event_loop_policy()

BACKEND_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://ham:ham_local_dev@localhost:5432/healthcare_test"
# Fixed, obviously-fake credentials: long enough to satisfy validation, never real secrets.
TEST_JWT_SECRET = "test-only-secret-not-used-anywhere-outside-the-suite"
DEFAULT_PASSWORD = "correct-horse-battery"


@pytest.fixture
def default_password() -> str:
    """The password `make_user` assigns, for tests that then log in as that user.

    Exposed as a fixture rather than an imported constant: `tests` is not an installed
    package, so cross-importing between test modules is fragile.
    """
    return DEFAULT_PASSWORD


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        app_env="test",
        log_level="WARNING",
        database_url=os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL),
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        # Short: a missing test database should fail the suite in seconds, not stall it.
        db_connect_timeout_seconds=5,
    )


@pytest.fixture(scope="session", autouse=True)
def migrated_schema(settings: Settings) -> None:
    """Rebuild the test schema from the migrations, once per session.

    Running the real migrations rather than `metadata.create_all` means the suite proves the
    migrations work — a schema that only exists in the models would deploy to nothing.
    Downgrading first guarantees a clean slate and exercises the downgrade path every run.
    """
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["db_url"] = settings.database_url
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    """A database handle independent of the application's own engine."""
    handle = Database(
        settings.database_url,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
    )
    try:
        yield handle
    finally:
        await handle.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(database: Database) -> None:
    """Empty every table before each test. `alembic_version` is untouched — it is not part
    of the model metadata, so the schema survives."""
    table_names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with database.engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db_session(database: Database) -> AsyncIterator[AsyncSession]:
    """A session for tests that set up or inspect rows directly."""
    session = database.session()
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app, with startup/shutdown actually executed.

    `ASGITransport` alone does not run the lifespan, which would leave `app.state.database`
    unset — so the lifespan context is entered explicitly.
    """
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client


MakeUser = Callable[..., Awaitable[User]]


@pytest_asyncio.fixture
async def make_user(db_session: AsyncSession) -> MakeUser:
    """Create a user of any role directly, bypassing the registration endpoint.

    Doctors and admins cannot be created through the API by design, so tests covering their
    routes need this.
    """

    async def _make(
        *,
        role: UserRole = UserRole.PATIENT,
        email: str | None = None,
        password: str = DEFAULT_PASSWORD,
        full_name: str = "Test Person",
        is_active: bool = True,
    ) -> User:
        user = User(
            # `.test` cannot be used: it is a reserved TLD that email validation rejects.
            email=email or f"{role.value}-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(password),
            full_name=full_name,
            role=role,
            is_active=is_active,
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        return user

    return _make


MakeDoctor = Callable[..., Awaitable[DoctorProfile]]


@pytest_asyncio.fixture
async def make_doctor(db_session: AsyncSession, make_user: MakeUser) -> MakeDoctor:
    """Create a doctor user together with their clinic profile."""

    async def _make(
        *,
        specialisation: str = "Cardiology",
        slot_duration_minutes: int = 30,
        full_name: str = "Dr Test",
    ) -> DoctorProfile:
        user = await make_user(role=UserRole.DOCTOR, full_name=full_name)
        profile = DoctorProfile(
            user_id=user.id,
            specialisation=specialisation,
            slot_duration_minutes=slot_duration_minutes,
        )
        db_session.add(profile)
        await db_session.commit()
        await db_session.refresh(profile)
        return profile

    return _make


@pytest.fixture
def auth_header(settings: Settings) -> Callable[[User], dict[str, str]]:
    """Build an Authorization header for a user, without going through login."""

    def _header(user: User) -> dict[str, str]:
        token = create_access_token(
            user_id=user.id,
            role=user.role,
            secret=settings.jwt_secret.get_secret_value(),
            algorithm=settings.jwt_algorithm,
            expires_in_minutes=settings.access_token_ttl_minutes,
        )
        return {"Authorization": f"Bearer {token}"}

    return _header
