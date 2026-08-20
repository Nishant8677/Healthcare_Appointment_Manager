"""Development entry point.

Prefer this over calling `uvicorn` directly on Windows: the event-loop policy has to be set
before uvicorn creates its loop, and uvicorn imports the application *inside* that loop — too
late for the application itself to fix it.

    python run.py
"""

from __future__ import annotations

from app.core.eventloop import configure_event_loop_policy

configure_event_loop_policy()

import uvicorn  # noqa: E402  - must be imported after the policy is set

from app.core.config import get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=not settings.is_production,
        log_config=None,  # the application configures logging itself
    )


if __name__ == "__main__":
    main()
