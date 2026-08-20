"""Liveness and readiness endpoints.

Split deliberately: `/healthz` answers "is the process alive" and must never touch the
database, so a database blip cannot cause the platform to kill a healthy container.
`/readyz` answers "can this instance serve traffic" and does check the database.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from starlette.requests import Request

from app import __version__
from app.api.deps import get_app_settings
from app.core.config import Settings
from app.core.db import Database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    database: Literal["up", "down"]


@router.get("/healthz", response_model=LivenessResponse, summary="Liveness probe")
async def liveness(settings: Settings = Depends(get_app_settings)) -> LivenessResponse:
    return LivenessResponse(version=__version__, environment=settings.app_env)


@router.get("/readyz", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(request: Request, response: Response) -> ReadinessResponse:
    database: Database = request.app.state.database
    try:
        await database.ping()
    except Exception:
        # Log the failure but return a structured 503 rather than a 500: this is an expected
        # state during a database restart, not an application bug.
        logger.exception("readiness check failed: database unreachable")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="degraded", database="down")
    return ReadinessResponse(status="ready", database="up")
