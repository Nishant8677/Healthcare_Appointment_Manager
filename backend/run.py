"""Application entry point, for development and production alike.

    python run.py

Prefer this over calling `uvicorn` directly on Windows. Uvicorn builds its event loop from a
factory that hard-codes `ProactorEventLoop` there, which the async database driver refuses to
run on, so `loop=` below points it at ours instead. See `app.core.eventloop` for why the
policy alone is not enough.

One entry point rather than two. A separate production command would be a second place for
that to be forgotten — and it would be forgotten on Linux, where nothing goes wrong. The only
real differences between the environments are which address to bind and whether to watch for
file changes, and both are decided by configuration rather than by code.
"""

from __future__ import annotations

import os

from app.core.eventloop import LOOP_FACTORY_PATH, configure_event_loop_policy

configure_event_loop_policy()

import uvicorn  # noqa: E402  - must be imported after the policy is set

from app.core.config import get_settings  # noqa: E402

# Platforms assign a port and expect the process to bind every interface. Locally, binding
# only the loopback address keeps a development server off the local network.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def main() -> None:
    settings = get_settings()
    production = settings.is_production

    host = os.environ.get("HOST") or ("0.0.0.0" if production else DEFAULT_HOST)  # noqa: S104
    port = int(os.environ.get("PORT", DEFAULT_PORT))

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=host,
        port=port,
        # An import string: uvicorn resolves anything it does not recognise as a path to a
        # loop factory. Without this it builds a ProactorEventLoop on Windows whenever the
        # reloader is off, and the first database query fails.
        loop=LOOP_FACTORY_PATH,
        # Reload watches the filesystem and runs a supervisor process; both are wasted work
        # in a container that is replaced rather than edited.
        reload=not production,
        log_config=None,  # the application configures logging itself
    )


if __name__ == "__main__":
    main()
