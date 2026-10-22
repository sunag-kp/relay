from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from relay.api.app import create_app
from relay.config import Settings
from tests.integration.conftest import integration_settings

pytestmark = pytest.mark.integration


async def test_readiness_against_real_redis_and_postgres(settings: Settings) -> None:
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")
    assert response.status_code == 200, response.text
    assert response.json()["checks"] == {"redis": "ok", "postgres": "ok"}


async def test_readiness_reports_unreachable_redis() -> None:
    app = create_app(integration_settings(redis_url="redis://localhost:1/0"))
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["redis"].startswith("error")
    assert response.json()["checks"]["postgres"] == "ok"
