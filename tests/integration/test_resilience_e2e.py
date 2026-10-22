"""Chaos over real HTTP: break one mock provider at runtime and watch the gateway fall back,
trip the breaker, and recover once the provider heals."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.api.app import create_app
from tests.integration.conftest import integration_settings

pytestmark = pytest.mark.integration

KEY = "rk_chaos_test_key"
CATALOG = """
providers:
  - name: flaky
    kind: openai_compatible
    base_url: "${MOCK_URL}/p/chaos-flaky/v1"
    max_retries: 0
    breaker: {min_calls: 3, window_size: 5, open_seconds: 1, half_open_max_calls: 1}
  - name: steady
    kind: openai_compatible
    base_url: "${MOCK_URL}/p/chaos-steady/v1"
routes:
  - model: chaos
    targets:
      - {provider: flaky, model: f}
      - {provider: steady, model: s}
"""


async def test_breaker_trips_and_recovers(
    tmp_path: Path, mock_url: str, monkeypatch: pytest.MonkeyPatch, clean_db: AsyncEngine
) -> None:
    monkeypatch.setenv("MOCK_URL", mock_url)
    config = tmp_path / "chaos.yaml"
    config.write_text(CATALOG)
    app = create_app(integration_settings(relay_config_file=config, demo_api_key=SecretStr(KEY)))
    body = {"model": "chaos", "messages": [{"role": "user", "content": "hi"}]}
    auth = {"authorization": f"Bearer {KEY}"}

    async with (
        LifespanManager(app) as manager,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url="http://gw"
        ) as gw,
        httpx.AsyncClient(base_url=mock_url) as mock,
    ):
        health = app.state.container.chat.health
        await mock.put(
            "/_control/profiles/chaos-flaky", json={"failure_rate": 1.0, "latency_ms": 1}
        )
        try:
            served = []
            for _ in range(6):
                response = await gw.post("/v1/chat/completions", json=body, headers=auth)
                assert response.status_code == 200
                served.append(
                    (response.headers["x-relay-provider"], response.headers["x-relay-attempts"])
                )
            # First three requests try flaky, then fall back; then the breaker is open and
            # flaky is skipped without an attempt.
            assert served[:3] == [("steady", "2")] * 3
            assert served[3:] == [("steady", "1")] * 3
            assert health.snapshot()["providers"]["flaky"]["state"] == "open"

            # Heal the provider; after open_seconds one probe closes the breaker again.
            await mock.put("/_control/profiles/chaos-flaky", json={"failure_rate": 0.0})
            await asyncio.sleep(1.05)
            response = await gw.post("/v1/chat/completions", json=body, headers=auth)
            assert response.headers["x-relay-provider"] == "flaky"
            assert health.snapshot()["providers"]["flaky"]["state"] == "closed"
        finally:
            await mock.post("/_control/reset")
