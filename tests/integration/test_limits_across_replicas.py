"""Three independent gateway instances (each with its own process-level state, as separate
replicas would have) share one Redis: together they must admit exactly the key's limit.

If limits were kept per replica, each instance would admit the full limit and the total
would be three times too high."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.api.app import create_app
from relay.services.keys import KeySpec
from tests.integration.conftest import integration_settings

pytestmark = pytest.mark.integration

RPM = 20
BODY = {"model": "relay-mock", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
async def replicas(
    catalog_file: Path, clean_db: AsyncEngine
) -> AsyncIterator[tuple[FastAPI, list[httpx.AsyncClient]]]:
    apps = [
        create_app(integration_settings(relay_config_file=catalog_file, replica_id=f"replica-{i}"))
        for i in range(3)
    ]
    managers = [LifespanManager(app) for app in apps]
    for manager in managers:
        await manager.__aenter__()
    clients = [
        httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url=f"http://r{i}")
        for i, m in enumerate(managers)
    ]
    yield apps[0], clients
    for client in clients:
        await client.aclose()
    for manager in managers:
        await manager.__aexit__(None, None, None)


async def new_key(app: FastAPI) -> dict[str, str]:
    _, plaintext = await app.state.container.keys.create(
        KeySpec(name="shared", owner="team", rpm_limit=RPM, tpd_limit=10**7)
    )
    return {"authorization": f"Bearer {plaintext}"}


async def test_round_robin_across_replicas_shares_one_limit(
    replicas: tuple[FastAPI, list[httpx.AsyncClient]],
) -> None:
    app, clients = replicas
    auth = await new_key(app)
    started = time.perf_counter()
    statuses: list[tuple[str, int]] = []
    for i in range(RPM + 10):
        response = await clients[i % 3].post("/v1/chat/completions", json=BODY, headers=auth)
        statuses.append((response.headers["x-relay-replica"], response.status_code))
    elapsed = time.perf_counter() - started

    ok = Counter(replica for replica, status in statuses if status == 200)
    assert set(ok) == {"replica-0", "replica-1", "replica-2"}
    assert RPM <= sum(ok.values()) <= RPM + int(elapsed * RPM / 60)
    assert statuses[-1][1] == 429


async def test_concurrent_burst_across_replicas_admits_exactly_the_limit(
    replicas: tuple[FastAPI, list[httpx.AsyncClient]],
) -> None:
    app, clients = replicas
    auth = await new_key(app)
    started = time.perf_counter()
    responses = await asyncio.gather(
        *(
            clients[i % 3].post("/v1/chat/completions", json=BODY, headers=auth)
            for i in range(RPM * 3)
        )
    )
    elapsed = time.perf_counter() - started

    ok = [r for r in responses if r.status_code == 200]
    limited = [r for r in responses if r.status_code == 429]
    assert len(ok) + len(limited) == len(responses)
    # Exactly the limit, plus whatever the bucket refilled while the burst ran (RPM/60 per s).
    assert RPM <= len(ok) <= RPM + int(elapsed * RPM / 60)
    assert all(int(r.headers["retry-after"]) >= 1 for r in limited)
