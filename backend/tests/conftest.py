"""Shared test fixtures.

Tests run against a real Postgres database (`healthcare_test`), never SQLite: the booking
logic this project is judged on depends on Postgres row locking and partial unique indexes,
so a substitute engine would let broken concurrency code pass.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import Settings
from app.core.eventloop import configure_event_loop_policy
from app.main import create_app

# Must run before pytest-asyncio creates any event loop: on Windows the default loop cannot
# run the async database driver.
configure_event_loop_policy()

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://ham:ham_local_dev@localhost:5432/healthcare_test"
# Fixed, obviously-fake secret: long enough to satisfy validation, never a real credential.
TEST_JWT_SECRET = "test-only-secret-not-used-anywhere-outside-the-suite"


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
