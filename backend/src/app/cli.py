"""Command-line administration.

Exists to solve a bootstrapping problem: admins cannot self-register (that would let anyone
become an administrator of a medical system), and only an admin can create other accounts —
so the very first admin has to come from outside the API.

    ADMIN_PASSWORD='...' python -m app.cli create-admin --email a@clinic.com --name "Ops"

It also carries the demo seed, for the same reason: populating a fresh deployment is
something you do to a database from outside, once, not something the API should expose.

    DEMO_PASSWORD='...' python -m app.cli seed-demo
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy.exc import IntegrityError

from app.core.eventloop import configure_event_loop_policy

# Before any loop exists: the async database driver cannot run on Windows' default loop.
configure_event_loop_policy()

from app.core.config import get_settings  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.models.user import User  # noqa: E402
from app.schemas.auth import PASSWORD_MIN_LENGTH  # noqa: E402
from app.seed import SeedRefused, seed_demo  # noqa: E402
from app.services.auth_service import normalise_email  # noqa: E402

logger = logging.getLogger(__name__)

PASSWORD_ENV_VAR = "ADMIN_PASSWORD"  # noqa: S105 - the variable's name, not its value
DEMO_PASSWORD_ENV_VAR = "DEMO_PASSWORD"  # noqa: S105


async def create_admin(*, email: str, full_name: str, password: str) -> int:
    """Create an admin account. Returns a process exit code."""
    settings = get_settings()
    database = Database(
        settings.database_url,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
    )

    try:
        session = database.session()
        try:
            session.add(
                User(
                    email=normalise_email(email),
                    password_hash=hash_password(password),
                    full_name=full_name,
                    role=UserRole.ADMIN,
                )
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            print(f"An account already exists for {email}.", file=sys.stderr)
            return 1
        finally:
            await session.close()
    finally:
        await database.dispose()

    print(f"Admin account created for {email}.")
    return 0


def _read_password(variable: str, purpose: str) -> str:
    """Take a password from the environment, never from an argument.

    A password passed as a command-line flag is visible in shell history and to anyone who
    can list processes on the machine.
    """
    password = os.environ.get(variable)
    if not password:
        raise SystemExit(f"Set {variable} to {purpose} before running this command.")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise SystemExit(f"{variable} must be at least {PASSWORD_MIN_LENGTH} characters.")
    return password


async def seed(*, password: str) -> int:
    """Fill an empty database with demo accounts and appointments."""
    settings = get_settings()
    database = Database(
        settings.database_url,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
    )

    try:
        session = database.session()
        try:
            report = await seed_demo(session, settings=settings, password=password)
        except SeedRefused as refusal:
            print(str(refusal), file=sys.stderr)
            return 1
        finally:
            await session.close()
    finally:
        await database.dispose()

    for line in report.lines:
        print(line)
    if report.created:
        print()
        print(
            f"Every account above uses the password from {DEMO_PASSWORD_ENV_VAR}. "
            "Record it - it is not stored anywhere else."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create-admin", help="Create the first admin account.")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True, dest="full_name")

    subcommands.add_parser(
        "seed-demo",
        help="Fill an empty database with demo accounts and appointments.",
    )

    args = parser.parse_args(argv)

    if args.command == "create-admin":
        return asyncio.run(
            create_admin(
                email=args.email,
                full_name=args.full_name,
                password=_read_password(PASSWORD_ENV_VAR, "the new admin's password"),
            )
        )

    if args.command == "seed-demo":
        return asyncio.run(
            seed(
                password=_read_password(
                    DEMO_PASSWORD_ENV_VAR, "the password every demo account will share"
                )
            )
        )

    parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
