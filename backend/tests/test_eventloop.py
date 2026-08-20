"""The event loop the server actually runs on.

This file exists because of a bug that only appeared with the reloader switched off, on
Windows, which is not where anything is deployed and therefore not where anyone was looking.
Uvicorn does not build its loop from the policy — it uses a factory that hard-codes
`ProactorEventLoop` on Windows unless it is running a reload subprocess — so a development
server worked and a production-style run failed on its first database query.

The assertions are written to mean something on every platform rather than being skipped
outside Windows, because the point is "the loop the database driver gets is one it can use".
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.core.eventloop import LOOP_FACTORY_PATH, loop_factory


def test_the_factory_never_hands_the_driver_a_proactor_loop() -> None:
    """psycopg raises `InterfaceError` on the first connection if it gets one."""
    loop = loop_factory()
    try:
        assert not isinstance(loop, getattr(asyncio, "ProactorEventLoop", ()))
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_uvicorn_resolves_the_factory_path() -> None:
    """The path is a string, so nothing but this catches a rename until the server fails to
    start — and it would fail at deploy time, in a container, with a stack trace nobody is
    watching."""
    config = uvicorn.Config("app.main:create_app", factory=True, loop=LOOP_FACTORY_PATH)

    resolved = config.get_loop_factory()

    assert resolved is loop_factory


def test_uvicorns_own_default_is_the_one_that_would_break_windows() -> None:
    """Pins the reason this module exists. If uvicorn ever stops hard-coding a Proactor loop
    for the non-subprocess case, this fails and the workaround can go."""
    from uvicorn.loops.asyncio import asyncio_loop_factory

    if sys.platform == "win32":
        assert asyncio_loop_factory(use_subprocess=False) is asyncio.ProactorEventLoop
        # The reloader runs in a subprocess, which is why development never showed the bug.
        assert asyncio_loop_factory(use_subprocess=True) is asyncio.SelectorEventLoop
    else:
        assert asyncio_loop_factory(use_subprocess=False) is asyncio.SelectorEventLoop
