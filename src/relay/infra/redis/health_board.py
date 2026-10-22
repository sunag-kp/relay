from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis


class RedisHealthBoard:
    """Each replica writes its provider-health snapshot under its own key with a TTL, so the
    dashboard sees every live replica and a dead one simply expires."""

    def __init__(self, redis: Redis, namespace: str = "relay") -> None:
        self._redis = redis
        self._prefix = f"{namespace}:health:"

    async def publish(self, replica: str, snapshot: dict[str, Any], ttl_s: int) -> None:
        await self._redis.set(self._prefix + replica, json.dumps(snapshot), ex=ttl_s)

    async def read_all(self) -> dict[str, dict[str, Any]]:
        keys = [key async for key in self._redis.scan_iter(match=self._prefix + "*", count=200)]
        if not keys:
            return {}
        values = await self._redis.mget(keys)
        out: dict[str, dict[str, Any]] = {}
        for key, value in zip(keys, values, strict=True):
            if value is None:
                continue
            name = key.decode() if isinstance(key, bytes) else str(key)
            out[name.removeprefix(self._prefix)] = json.loads(value)
        return out
