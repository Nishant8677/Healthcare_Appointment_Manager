"""Health endpoint behaviour and the request-correlation contract."""

from __future__ import annotations

from httpx import AsyncClient

from app import __version__


async def test_liveness_reports_version_and_environment(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "environment": "test",
    }


async def test_readiness_confirms_database_connection(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "up"}


async def test_response_echoes_supplied_request_id(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "trace-me-123"})

    assert response.headers["X-Request-ID"] == "trace-me-123"


async def test_response_generates_request_id_when_absent(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.headers.get("X-Request-ID")


async def test_openapi_schema_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["version"] == __version__
