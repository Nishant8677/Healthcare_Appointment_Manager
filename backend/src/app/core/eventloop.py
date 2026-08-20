"""Choosing an event loop the database driver can use.

psycopg's async driver refuses to run on Windows' `ProactorEventLoop` and raises
`InterfaceError` on the first connection attempt. Linux — where this deploys — is unaffected,
so everything here is a development-environment fix rather than a runtime dependency.

Two mechanisms are needed, for two different callers, and the difference is easy to get wrong:

* **Setting the policy** works for anything that creates its own loop through `asyncio.run` —
  Alembic's env, the CLI, the test suite — because `asyncio.run` asks the current policy for a
  loop. It must happen before any loop exists, which is why those entry points call it first
  thing.

* **The policy does not reach uvicorn.** Since 0.36 uvicorn builds its loop from a *factory*
  rather than the policy, and its built-in factory hard-codes `ProactorEventLoop` on Windows
  unless it is running the reloader in a subprocess. So a development server (reload on)
  worked while a production-style run (reload off) failed on the first query — on Windows
  only, and therefore not on the machine where anyone would notice. `loop_factory` below is
  passed to uvicorn by import string so it uses ours instead.
"""

from __future__ import annotations

import asyncio
import sys

# What uvicorn is given as its `loop` setting. Kept next to the function so the two
# cannot drift: a stale path here is a startup failure, not a type error.
LOOP_FACTORY_PATH = "app.core.eventloop:loop_factory"


def configure_event_loop_policy() -> None:
    """Select an event loop policy the database driver can actually use.

    For entry points that create their own loop. Uvicorn needs `loop_factory` instead.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def loop_factory() -> asyncio.AbstractEventLoop:
    """Build the loop uvicorn should run on.

    Referenced as a string — `app.core.eventloop:loop_factory` — because uvicorn resolves an
    unrecognised `loop` setting as an import path and calls what it finds. On anything but
    Windows this is exactly the default.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
