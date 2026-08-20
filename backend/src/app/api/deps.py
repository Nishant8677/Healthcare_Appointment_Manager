"""Shared FastAPI dependencies.

Request handlers resolve configuration, sessions and the current user through these rather
than reaching for module-level globals, so an application built with injected settings (as
the test suite does) behaves consistently everywhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.config import Settings
from app.core.db import Database, get_session
from app.core.security import TokenError, decode_access_token
from app.models.enums import UserRole
from app.models.user import User

__all__ = [
    "Database",
    "get_app_settings",
    "get_current_user",
    "get_session",
    "require_roles",
]

# auto_error=False so a missing header produces our own 401 with a WWW-Authenticate
# challenge, rather than FastAPI's bare 403.
_bearer_scheme = HTTPBearer(auto_error=False, description="Access token from /auth/login")

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_app_settings(request: Request) -> Settings:
    """Return the settings bound to this application instance."""
    settings: Settings = request.app.state.settings
    return settings


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> User:
    """Resolve the signed-in user from the bearer token.

    Every failure returns the same 401: a token that is missing, expired, forged or points
    at a deleted account should be indistinguishable to the caller.
    """
    if credentials is None:
        raise _UNAUTHENTICATED

    try:
        claims = decode_access_token(
            credentials.credentials,
            secret=settings.jwt_secret.get_secret_value(),
            algorithm=settings.jwt_algorithm,
        )
        user_id = uuid.UUID(claims["sub"])
    except (TokenError, ValueError, KeyError):
        raise _UNAUTHENTICATED from None

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise _UNAUTHENTICATED

    return user


def require_roles(*allowed: UserRole) -> Callable[..., Awaitable[User]]:
    """Build a dependency that admits only the listed roles.

    Used as `Depends(require_roles(UserRole.ADMIN))` on a route, which keeps the permission
    rule in the route signature — visible in the generated API documentation — instead of
    buried in an `if` at the top of the handler.
    """

    async def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account does not have access to this resource.",
            )
        return user

    return dependency
