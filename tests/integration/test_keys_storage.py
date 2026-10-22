from __future__ import annotations

from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.domain.errors import AuthenticationFailed
from relay.domain.keys import hash_key
from relay.infra.postgres.keys import PostgresApiKeyRepository
from relay.infra.redis.key_cache import RedisApiKeyCache
from relay.services.keys import KeyService, KeySpec
from relay.services.ports import KeyChanges

pytestmark = pytest.mark.integration


async def test_key_lifecycle_against_postgres_and_redis(
    clean_db: AsyncEngine, redis: Redis
) -> None:
    repository = PostgresApiKeyRepository(clean_db)
    cache = RedisApiKeyCache(redis, ttl_s=60, namespace="relay-it")
    keys = KeyService(repository, cache)

    key, plaintext = await keys.create(
        KeySpec(
            name="search",
            owner="team-search",
            rpm_limit=60,
            tpd_limit=100_000,
            monthly_budget_usd=Decimal("25.00"),
            allowed_models=("relay-mock",),
        )
    )
    authenticated = await keys.authenticate(plaintext)
    assert authenticated == key
    # The record is now cached in Redis, and round-trips losslessly.
    assert await cache.get(hash_key(plaintext)) == key

    updated = await keys.update(
        key.id, KeyChanges(rpm_limit=5, clear_budget=True, clear_allowed_models=True)
    )
    assert (updated.rpm_limit, updated.monthly_budget_usd, updated.allowed_models) == (
        5,
        None,
        None,
    )
    assert await cache.get(hash_key(plaintext)) is None  # invalidated
    assert (await keys.authenticate(plaintext)).rpm_limit == 5

    revoked = await keys.revoke(key.id)
    assert revoked.revoked_at is not None
    with pytest.raises(AuthenticationFailed):
        await keys.authenticate(plaintext)
    assert await keys.list() == []
    assert len(await keys.list(include_revoked=True)) == 1
    # revoking twice is a no-op
    assert (await keys.revoke(key.id)).revoked_at == revoked.revoked_at


async def test_negative_cache_entry_expires(clean_db: AsyncEngine, redis: Redis) -> None:
    cache = RedisApiKeyCache(redis, ttl_s=60, namespace="relay-it")
    keys = KeyService(PostgresApiKeyRepository(clean_db), cache)
    with pytest.raises(AuthenticationFailed):
        await keys.authenticate("rk_missing")
    assert await cache.get(hash_key("rk_missing")) is False
    ttl = await redis.ttl("relay-it:apikey:" + hash_key("rk_missing"))
    assert 0 < ttl <= 10


async def test_concurrent_ensure_from_many_replicas_creates_one_key(
    clean_db: AsyncEngine, redis: Redis
) -> None:
    import asyncio

    services = [
        KeyService(PostgresApiKeyRepository(clean_db), RedisApiKeyCache(redis, 60, "relay-it"))
        for _ in range(5)
    ]
    spec = KeySpec(name="demo", owner="demo", rpm_limit=10, tpd_limit=1000)
    results = await asyncio.gather(*(s.ensure("rk_shared_demo", spec) for s in services))
    assert len({key.id for key in results}) == 1
    assert len(await services[0].list()) == 1
