"""The whole gateway on real sockets: client -> gateway -> mock provider, with real
Postgres and Redis behind it."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.api.app import create_app
from tests.integration.conftest import integration_settings
from tests.integration.servers import serve

pytestmark = pytest.mark.integration

DEMO_KEY = "rk_integration_demo_key"


def message(text: str = "hello gateway") -> dict[str, object]:
    return {"role": "user", "content": text}


async def test_completion_and_true_streaming_through_real_http(
    catalog_file: Path, clean_db: AsyncEngine
) -> None:
    settings = integration_settings(
        relay_config_file=catalog_file, demo_api_key=SecretStr(DEMO_KEY)
    )
    app = create_app(settings)
    async with serve(app) as gateway, httpx.AsyncClient(base_url=gateway, timeout=10) as client:
        auth = {"authorization": f"Bearer {DEMO_KEY}", "cache-control": "no-cache, no-store"}

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "relay-mock", "messages": [message()]},
            headers=auth,
        )
        assert response.status_code == 200, response.text
        assert response.headers["x-relay-provider"] == "mock-alpha"
        assert response.json()["choices"][0]["message"]["content"].startswith(
            "[alpha] You said: hello gateway"
        )

        # Streaming: the first token must reach the client long before the last one
        # (alpha's profile spaces chunks 20 ms apart), proving nothing buffers the stream.
        started = time.perf_counter()
        arrivals: list[float] = []
        text = []
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "relay-mock", "messages": [message()], "stream": True},
            headers=auth,
        ) as stream:
            assert stream.status_code == 200
            async for line in stream.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                arrivals.append(time.perf_counter() - started)
                event = json.loads(line.removeprefix("data: "))
                text.extend(choice["delta"].get("content", "") for choice in event["choices"])
        assert "".join(text).startswith("[alpha] You said: hello gateway")
        assert arrivals[-1] - arrivals[1] > 0.2


async def test_anthropic_translation_end_to_end(catalog_file: Path, clean_db: AsyncEngine) -> None:
    settings = integration_settings(
        relay_config_file=catalog_file, demo_api_key=SecretStr(DEMO_KEY)
    )
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "relay-claude",
                    "messages": [{"role": "system", "content": "be brief"}, message("bonjour")],
                    "temperature": 0.2,  # dropped by the adapter: the Messages API rejects it
                    "max_tokens": 6,
                },
                headers={"authorization": f"Bearer {DEMO_KEY}"},
            )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["finish_reason"] == "length"
    assert body["choices"][0]["message"]["content"].startswith("[claude] You said: bonjour")
    assert body["usage"]["completion_tokens"] == 6
