"""Redis is only a cache for key records: when it fails, authentication must still work."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from relay.infra.redis.key_cache import RedisApiKeyCache
from relay.services.keys import KeyService, KeySpec
from tests.fakes import FakeKeyRepository


@pytest.fixture
async def unreachable_redis() -> AsyncIterator[Redis]:
    redis = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.5)  # nothing listens
    yield redis
    await redis.aclose()


async def test_redis_failures_fall_through_to_the_repository(unreachable_redis: Redis) -> None:
    keys = KeyService(FakeKeyRepository(), RedisApiKeyCache(unreachable_redis, ttl_s=60))
    key, plaintext = await keys.create(KeySpec(name="k", owner="o", rpm_limit=1, tpd_limit=10))

    assert (await keys.authenticate(plaintext)).id == key.id
    revoked = await keys.revoke(key.id)  # committed even though the cache can't be cleared
    assert revoked.revoked_at is not None
