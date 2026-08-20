"""Alembic environment, wired to application settings and metadata."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.eventloop import configure_event_loop_policy
from app.models import Base

configure_event_loop_policy()

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` is essential, not cosmetic. The default (True) turns
    # off every logger that already exists, so when the test suite runs migrations in-process
    # the application's own loggers go silent for the rest of the session — which silently
    # disabled the request-correlation logging and its regression test.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the target database.

    Precedence: a URL injected programmatically (the test suite migrates a throwaway
    database this way), then an explicit `-x url=...` on the command line, then the
    application's configured database.
    """
    injected = config.attributes.get("db_url")
    if isinstance(injected, str) and injected:
        return injected

    override = context.get_x_argument(as_dictionary=True).get("url")
    return override or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Catch column type and default drift, not just added/removed tables.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=None)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
