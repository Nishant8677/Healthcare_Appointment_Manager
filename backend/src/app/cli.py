"""Command-line administration.

Exists to solve a bootstrapping problem: admins cannot self-register (that would let anyone
become an administrator of a medical system), and only an admin can create other accounts —
so the very first admin has to come from outside the API.

    ADMIN_PASSWORD='...' python -m app.cli create-admin --email a@clinic.com --name "Ops"

It also carries the demo seed, for the same reason: populating a fresh deployment is
something you do to a database from outside, once, not something the API should expose.

    DEMO_PASSWORD='...' python -m app.cli seed-demo

And a diagnostic, because "is the model actually wired up" is a question worth being able to
answer from a deployment's shell without booking an appointment to find out:

    python -m app.cli check-llm
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
from app.services.llm import LLMError, LLMRefusal, PreVisitSummary, build_llm_client  # noqa: E402
from app.services.summaries import PRE_VISIT_SYSTEM  # noqa: E402

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


async def check_llm() -> int:
    """Send one real request through the configured provider and report what came back.

    Exercises the whole path — settings, client selection, the request shape, the response
    walk and the schema validation — rather than just pinging the endpoint. A key that works
    for `curl` but not for this code is exactly the failure worth catching before a patient's
    booking depends on it.
    """
    settings = get_settings()
    print(f"provider: {settings.llm_provider}")
    print(f"model:    {settings.llm_model}")
    print()

    try:
        client = build_llm_client(settings)
    except ValueError as error:
        print(f"not configured: {error}", file=sys.stderr)
        return 1

    if settings.llm_provider == "stub":
        print("This is the stub. Set LLM_PROVIDER and LLM_API_KEY to call a real model.")
        return 0

    # Deliberately mild and fictional: this runs against a real provider, and a diagnostic
    # should not be sending invented symptoms that read as a real person's notes.
    probe = "Analyse these symptoms.\n\nSYMPTOMS\n---\nA mild sore throat for two days.\n---"

    try:
        summary = await client.generate(
            system=PRE_VISIT_SYSTEM,
            user=probe,
            output_model=PreVisitSummary,
            max_tokens=settings.llm_max_output_tokens,
        )
    except LLMRefusal as error:
        print(f"the model declined: {error}", file=sys.stderr)
        return 1
    except LLMError as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1

    print(f"urgency:   {summary.urgency}")
    print(f"complaint: {summary.chief_complaint}")
    for question in summary.suggested_questions:
        print(f"  - {question}")
    print()
    print("The model is reachable and its answers match the required shape.")
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

    subcommands.add_parser(
        "check-llm",
        help="Send one real request through the configured model provider.",
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

    if args.command == "check-llm":
        return asyncio.run(check_llm())

    parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
