from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from asgi_lifespan import LifespanManager

from relay.api.app import create_app
from relay.bootstrap import Container
from relay.config import Settings
from relay.services.background import PeriodicTasks


class _StubContainer:
    def __init__(self, checks: dict[str, Any]) -> None:
        self.settings = Settings(replica_id="replica-test")
        self.readiness_checks = checks
        self.periodic = PeriodicTasks()

    async def aclose(self) -> None:
        return None


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise ConnectionError("redis down")


@pytest.fixture
async def make_client() -> AsyncIterator[Any]:
    managers: list[LifespanManager] = []

    async def factory(checks: dict[str, Any]) -> httpx.AsyncClient:
        app = create_app(container=cast("Container", _StubContainer(checks)))
        manager = LifespanManager(app)
        managers.append(manager)
        await manager.__aenter__()
        transport = httpx.ASGITransport(app=manager.app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    yield factory
    for manager in managers:
        await manager.__aexit__(None, None, None)


async def test_liveness_does_not_touch_dependencies(make_client: Any) -> None:
    client = await make_client({"redis": _boom})
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "replica": "replica-test"}
    assert response.headers["x-relay-replica"] == "replica-test"
    assert response.headers["x-request-id"]


async def test_readiness_reports_each_dependency(make_client: Any) -> None:
    client = await make_client({"redis": _ok, "postgres": _ok})
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"] == {"redis": "ok", "postgres": "ok"}


async def test_readiness_fails_when_a_dependency_is_down(make_client: Any) -> None:
    client = await make_client({"redis": _boom, "postgres": _ok})
    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["redis"] == "error: ConnectionError"


async def test_client_request_id_is_echoed_and_unsafe_ids_replaced(make_client: Any) -> None:
    client = await make_client({})
    good = await client.get("/healthz", headers={"x-request-id": "abc-123"})
    assert good.headers["x-request-id"] == "abc-123"
    bad = await client.get("/healthz", headers={"x-request-id": "<script>"})
    assert bad.headers["x-request-id"] != "<script>"


async def test_metrics_endpoint_serves_prometheus_text(make_client: Any) -> None:
    client = await make_client({})
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text
