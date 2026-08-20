"""A background loop that runs one database pass on an interval.

Both background jobs — delivering notifications and generating summaries — have the same
shape: open a session, do one bounded pass, close it, wait, repeat. The loop itself is the
part with the subtle requirements (survive a failing pass, shut down cleanly, never let one
error end the process), so it is written once here rather than twice.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database

logger = logging.getLogger(__name__)

# One pass: given a session, do some work and report what happened.
Pass = Callable[[AsyncSession], Awaitable[dict[str, int] | Any]]


class PollingWorker:
    """Runs `run_pass` every `poll_seconds` until stopped."""

    def __init__(
        self,
        name: str,
        database: Database,
        run_pass: Pass,
        *,
        poll_seconds: float,
    ) -> None:
        self._name = name
        self._database = database
        self._run_pass = run_pass
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=self._name)
        logger.info(
            "background worker started",
            extra={"worker": self._name, "poll_seconds": self._poll_seconds},
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("background worker stopped", extra={"worker": self._name})

    async def _run(self) -> None:
        while True:
            try:
                session = self._database.session()
                try:
                    result = await self._run_pass(session)
                    if isinstance(result, dict) and any(result.values()):
                        logger.info(
                            "background pass completed",
                            extra={"worker": self._name, **result},
                        )
                finally:
                    await session.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop must outlive any single failure: a database blip should not stop
                # this work for the rest of the process's life.
                logger.exception("background pass failed", extra={"worker": self._name})

            await asyncio.sleep(self._poll_seconds)
