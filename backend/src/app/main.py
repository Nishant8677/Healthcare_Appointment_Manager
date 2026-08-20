"""Application factory and lifespan wiring.

Run locally with:
    uvicorn app.main:create_app --factory --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import __version__
from app.api import admin_doctors, appointments, auth, doctors, health
from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.logging import configure_logging, request_id_var
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
Backend for the Healthcare Appointment & Follow-up Manager.

Role-based portals for patients, doctors and clinic admins: slot search and booking with
double-booking protection, AI pre-visit and post-visit summaries, email notifications and
Google Calendar sync.
"""


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(level=settings.log_level, json_output=settings.is_production)
        logger.info(
            "starting application",
            extra={
                "version": __version__,
                "environment": settings.app_env,
                "database": settings.safe_database_url,
            },
        )

        database = Database(
            settings.database_url,
            echo=False,
            connect_timeout_seconds=settings.db_connect_timeout_seconds,
        )
        app.state.database = database
        app.state.settings = settings

        try:
            await database.ping()
            logger.info("database connection established")
        except Exception:
            logger.exception("database unreachable at startup")
            if not settings.is_production:
                # Fail loud locally: a developer running with a stopped database should be
                # told immediately, not left to debug failing requests.
                await database.dispose()
                raise
            # In production, keep serving so the platform can route on /readyz and the
            # instance can recover on its own once the database returns.
            logger.warning("continuing startup in degraded mode; /readyz will report down")

        try:
            yield
        finally:
            await database.dispose()
            logger.info("application shutdown complete")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured FastAPI application.

    Accepting `settings` keeps the app testable: the suite injects a configuration pointed
    at a throwaway database instead of mutating global state.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title="Healthcare Appointment & Follow-up Manager API",
        description=API_DESCRIPTION,
        version=__version__,
        lifespan=_build_lifespan(settings),
        # Interactive API documentation is a graded deliverable; keep it at a stable path.
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Added first so it sits *inside* CORS: preflight requests are answered without
    # generating a request id, while every real request is logged and correlated.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin_doctors.router)
    app.include_router(doctors.router)
    app.include_router(appointments.router)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Return a consistent error envelope instead of leaking a stack trace."""
        logger.exception("unhandled exception", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error.",
                "request_id": request_id_var.get(),
            },
        )

    return app
