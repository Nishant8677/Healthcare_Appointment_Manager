"""Shared FastAPI dependencies.

Request handlers resolve configuration and database sessions through these rather than
reaching for module-level globals, so an application built with injected settings (as the
test suite does) behaves consistently everywhere.
"""

from __future__ import annotations

from starlette.requests import Request

from app.core.config import Settings
from app.core.db import Database, get_session

__all__ = ["Database", "get_app_settings", "get_session"]


def get_app_settings(request: Request) -> Settings:
    """Return the settings bound to this application instance."""
    settings: Settings = request.app.state.settings
    return settings
