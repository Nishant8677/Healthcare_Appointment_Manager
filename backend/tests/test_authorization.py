"""Role-based access control.

`require_roles` has no route of its own until Phase 2, so it is exercised here against a
throwaway route mounted on a real application — testing the dependency itself rather than
shipping a placeholder endpoint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import require_roles
from app.core.config import Settings
from app.main import create_app
from app.models import User, UserRole

MakeUser = Callable[..., Awaitable[User]]

ADMIN_ONLY = "/_test/admin-only"
CLINICAL_STAFF = "/_test/clinical-staff"


@pytest.fixture
def guarded_app(settings: Settings) -> FastAPI:
    app = create_app(settings)
    router = APIRouter()

    @router.get(ADMIN_ONLY)
    async def admin_only(user: User = Depends(require_roles(UserRole.ADMIN))) -> dict[str, str]:
        return {"seen_by": user.role.value}

    @router.get(CLINICAL_STAFF)
    async def clinical_staff(
        user: User = Depends(require_roles(UserRole.DOCTOR, UserRole.ADMIN)),
    ) -> dict[str, str]:
        return {"seen_by": user.role.value}

    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def guarded_client(guarded_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with guarded_app.router.lifespan_context(guarded_app):
        transport = ASGITransport(app=guarded_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client


async def test_admin_reaches_an_admin_only_route(
    guarded_client: AsyncClient,
    make_user: MakeUser,
    auth_header: Callable[[User], dict[str, str]],
) -> None:
    admin = await make_user(role=UserRole.ADMIN)

    response = await guarded_client.get(ADMIN_ONLY, headers=auth_header(admin))

    assert response.status_code == 200
    assert response.json() == {"seen_by": "admin"}


@pytest.mark.parametrize("role", [UserRole.PATIENT, UserRole.DOCTOR])
async def test_other_roles_are_forbidden_from_an_admin_only_route(
    guarded_client: AsyncClient,
    make_user: MakeUser,
    auth_header: Callable[[User], dict[str, str]],
    role: UserRole,
) -> None:
    user = await make_user(role=role)

    response = await guarded_client.get(ADMIN_ONLY, headers=auth_header(user))

    assert response.status_code == 403


@pytest.mark.parametrize("role", [UserRole.DOCTOR, UserRole.ADMIN])
async def test_a_route_can_admit_several_roles(
    guarded_client: AsyncClient,
    make_user: MakeUser,
    auth_header: Callable[[User], dict[str, str]],
    role: UserRole,
) -> None:
    user = await make_user(role=role)

    response = await guarded_client.get(CLINICAL_STAFF, headers=auth_header(user))

    assert response.status_code == 200


async def test_patient_is_still_refused_by_a_multi_role_route(
    guarded_client: AsyncClient,
    make_user: MakeUser,
    auth_header: Callable[[User], dict[str, str]],
) -> None:
    patient = await make_user(role=UserRole.PATIENT)

    response = await guarded_client.get(CLINICAL_STAFF, headers=auth_header(patient))

    assert response.status_code == 403


async def test_missing_token_on_a_guarded_route_is_unauthenticated_not_forbidden(
    guarded_client: AsyncClient,
) -> None:
    """401 and 403 mean different things: "who are you" versus "not allowed"."""
    response = await guarded_client.get(ADMIN_ONLY)

    assert response.status_code == 401
