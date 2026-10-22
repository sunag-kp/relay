from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.api.app import create_app
from relay.domain.usage import BudgetAlert, UsageEvent, roll_up
from relay.infra.postgres.usage import PostgresAlertStore, PostgresUsageStore
from relay.infra.redis.health_board import RedisHealthBoard
from tests.integration.conftest import integration_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def metering_db(clean_db: AsyncEngine) -> AsyncEngine:
    async with clean_db.begin() as conn:
        await conn.execute(text("TRUNCATE usage_events, usage_daily, budget_alerts"))
    return clean_db


def event(key: uuid.UUID, **overrides: object) -> UsageEvent:
    values: dict[str, object] = {
        "request_id": uuid.uuid4().hex,
        "api_key_id": key,
        "owner": "team",
        "route": "relay-mock",
        "provider": "alpha",
        "provider_model": "a1",
        "status": "ok",
        "cache": "miss",
        "stream": False,
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "cost_usd": Decimal("0.00012345"),
        "latency_ms": 42,
        "attempts": 1,
        "replica": "it",
    }
    values.update(overrides)
    return UsageEvent(**values)  # type: ignore[arg-type]


async def test_events_and_additive_daily_rollups(metering_db: AsyncEngine) -> None:
    store = PostgresUsageStore(metering_db)
    key = uuid.uuid4()
    first = [
        event(key),
        event(key, cache="exact", cost_usd=Decimal(0), saved_usd=Decimal("0.0001")),
    ]
    second = [
        event(
            key, status="upstream_error", prompt_tokens=0, completion_tokens=0, cost_usd=Decimal(0)
        )
    ]
    # two "replicas" writing the same day/key/route/provider concurrently
    await asyncio.gather(store.write(first, roll_up(first)), store.write(second, roll_up(second)))

    today = datetime.now(UTC).date()
    [row] = await store.daily(today, today, key)
    assert (row.requests, row.errors, row.cache_hits) == (3, 1, 1)
    assert row.prompt_tokens == 14
    assert row.cost_usd == Decimal("0.00012345")
    assert row.saved_usd == Decimal("0.0001")
    assert row.latency_ms_sum == 126

    recent = await store.recent(10, key)
    assert len(recent) == 3
    assert recent[0].request_id == second[0].request_id  # newest first
    assert await store.daily(today, today, uuid.uuid4()) == []

    old = [event(key, created_at=datetime.now(UTC) - timedelta(days=40))]
    await store.write(old, roll_up(old))
    assert await store.purge_before(datetime.now(UTC) - timedelta(days=30)) == 1


async def test_alert_store_deduplicates_across_replicas(metering_db: AsyncEngine) -> None:
    store = PostgresAlertStore(metering_db)
    alert = BudgetAlert(uuid.uuid4(), "k", "team", "2026-10", 80, Decimal("8.1"), Decimal("10"))
    results = await asyncio.gather(*(store.insert_if_new(alert) for _ in range(5)))
    assert sorted(results) == [False, False, False, False, True]
    [stored] = await store.recent(10)
    assert stored.threshold_pct == 80
    assert stored.spend_usd == Decimal("8.1")


async def test_health_board_entries_expire(redis: Redis) -> None:
    board = RedisHealthBoard(redis, namespace=f"relay-it-{uuid.uuid4().hex[:6]}")
    await board.publish("r1", {"providers": {"a": {"state": "open"}}}, ttl_s=1)
    await board.publish("r2", {"providers": {}}, ttl_s=30)
    assert set(await board.read_all()) == {"r1", "r2"}
    await asyncio.sleep(1.1)
    assert set(await board.read_all()) == {"r2"}


async def test_traffic_shows_up_in_the_usage_summary(
    catalog_file: Path, metering_db: AsyncEngine
) -> None:
    key = "rk_metering_e2e"
    settings = integration_settings(
        relay_config_file=catalog_file, demo_api_key=SecretStr(key), admin_token=SecretStr("adm")
    )
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            for question in ("alpha?", "beta?", "alpha?"):
                body = {"model": "relay-mock", "messages": [{"role": "user", "content": question}]}
                response = await client.post(
                    "/v1/chat/completions", json=body, headers={"authorization": f"Bearer {key}"}
                )
                assert response.status_code == 200
                await app.state.container.background.drain()
            await app.state.container.usage.flush()
            summary = (
                await client.get(
                    "/admin/usage/summary?days=1", headers={"authorization": "Bearer adm"}
                )
            ).json()
    totals = summary["totals"]
    assert totals["requests"] == 3
    assert totals["cache_hits"] == 1
    assert totals["cost_usd"] > 0
    assert summary["by_key"][0]["name"] == "public demo"
    assert summary["by_provider"][0]["provider"] == "mock-alpha"
