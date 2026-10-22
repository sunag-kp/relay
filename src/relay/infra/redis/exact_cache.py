from __future__ import annotations

import json

from redis.asyncio import Redis
from redis.exceptions import RedisError

from relay.domain.cache import CachedCompletion
from relay.observability.log import get_logger

log = get_logger("relay.cache")


class RedisExactCache:
    """Exact-match response cache. Entries expire by TTL; Redis eviction (`maxmemory-policy
    volatile-lru` in compose) drops the least recently used ones first under memory pressure.

    A cache must never fail a request, so Redis errors are logged and treated as misses."""

    def __init__(self, redis: Redis, namespace: str = "relay") -> None:
        self._redis = redis
        self._prefix = f"{namespace}:cache:"

    async def get(self, key: str) -> CachedCompletion | None:
        try:
            raw = await self._redis.get(self._prefix + key)
        except RedisError as exc:
            log.warning("exact_cache_get_failed", error=str(exc))
            return None
        if raw is None:
            return None
        return CachedCompletion.from_json(json.loads(raw))

    async def put(self, key: str, entry: CachedCompletion, ttl_s: int) -> None:
        try:
            await self._redis.set(self._prefix + key, json.dumps(entry.to_json()), ex=ttl_s)
        except RedisError as exc:
            log.warning("exact_cache_put_failed", error=str(exc))

    async def flush(self) -> int:
        deleted = 0
        async for batch in _batches(self._redis.scan_iter(match=self._prefix + "*", count=500)):
            deleted += await self._redis.unlink(*batch)
        return deleted


async def _batches(keys, size: int = 500):  # type: ignore[no-untyped-def]
    batch = []
    async for key in keys:
        batch.append(key)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
