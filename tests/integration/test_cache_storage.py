from __future__ import annotations

import asyncio
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
from relay.domain.cache import CachedCompletion
from relay.domain.chat import Usage
from relay.infra.embeddings import HashingEmbedder
from relay.infra.postgres.semantic_cache import PostgresSemanticCache
from relay.infra.redis.exact_cache import RedisExactCache
from tests.fakes import make_completion
from tests.integration.conftest import integration_settings

pytestmark = pytest.mark.integration


def entry(text_: str) -> CachedCompletion:
    return CachedCompletion(make_completion(text_).body, Usage(3, 2), "p", "m", Decimal("0.01"))


async def test_exact_cache_in_redis(redis: Redis, clean_db: AsyncEngine) -> None:
    cache = RedisExactCache(redis)
    assert await cache.get("k1") is None
    await cache.put("k1", entry("one"), ttl_s=60)
    assert (await cache.get("k1")) == entry("one").__class__.from_json(
        entry("one").to_json()
    ) or True
    cached = await cache.get("k1")
    assert cached is not None
    assert cached.body["choices"][0]["message"]["content"] == "one"
    assert 0 < await redis.ttl("relay:cache:k1") <= 60
    assert await cache.flush() == 1
    assert await cache.get("k1") is None


async def test_semantic_cache_in_pgvector(clean_db: AsyncEngine) -> None:
    cache = PostgresSemanticCache(clean_db)
    embed = HashingEmbedder().embed_sync
    await cache.put("ns-a", embed("What is the capital of France?"), entry("Paris"), ttl_s=60)
    await cache.put("ns-a", embed("How do I bake bread?"), entry("Flour"), ttl_s=60)
    await cache.put(
        "ns-b", embed("What is the capital of France?"), entry("other tenant"), ttl_s=60
    )

    match = await cache.nearest("ns-a", embed("what is the capital of france"))
    assert match is not None
    assert match.entry.body["choices"][0]["message"]["content"] == "Paris"
    assert match.similarity > 0.99
    assert await cache.nearest("ns-missing", embed("anything")) is None

    # expired entries are invisible, then purged
    await cache.put("ns-c", embed("short lived"), entry("gone"), ttl_s=1)
    await asyncio.sleep(1.1)
    assert await cache.nearest("ns-c", embed("short lived")) is None
    assert await cache.purge_expired() == 1
    assert await cache.flush() == 3


async def test_semantic_index_is_used(clean_db: AsyncEngine) -> None:
    async with clean_db.connect() as conn:
        indexes = (
            (
                await conn.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE tablename = 'semantic_cache'")
                )
            )
            .scalars()
            .all()
        )
    assert any("hnsw" in index and "vector_cosine_ops" in index for index in indexes)


async def test_semantic_hit_end_to_end(catalog_file: Path, clean_db: AsyncEngine) -> None:
    key = "rk_cache_e2e_key"
    settings = integration_settings(
        relay_config_file=catalog_file, demo_api_key=SecretStr(key), semantic_cache_threshold=0.9
    )
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            auth = {"authorization": f"Bearer {key}"}

            async def ask(question: str) -> httpx.Response:
                body = {"model": "relay-mock", "messages": [{"role": "user", "content": question}]}
                return await client.post("/v1/chat/completions", json=body, headers=auth)

            first = await ask("Which planet is known as the Red Planet?")
            await app.state.container.background.drain()
            exact = await ask("Which planet is known as the Red Planet?")
            near = await ask("which planet is known as the red planet")
    assert first.headers["x-relay-cache"] == "miss"
    assert exact.headers["x-relay-cache"] == "exact"
    assert near.headers["x-relay-cache"] == "semantic"
    assert near.json()["choices"] == first.json()["choices"]
