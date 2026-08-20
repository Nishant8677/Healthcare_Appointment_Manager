"""Event-loop policy selection.

psycopg's async driver refuses to run on Windows' default `ProactorEventLoop` and raises
`InterfaceError` on the first connection attempt. Linux — where this deploys — is unaffected,
so this is a development-environment fix, not a runtime dependency.

The policy must be set *before* any event loop is created, which is why this is called from
dedicated entry points (`run.py`, Alembic's env, the test suite) rather than from application
code that is imported once the loop is already running.
"""

from __future__ import annotations

import asyncio
import sys


def configure_event_loop_policy() -> None:
    """Select an event loop policy the database driver can actually use."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
