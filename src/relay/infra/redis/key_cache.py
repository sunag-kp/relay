from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from relay.domain.keys import ApiKey

NEGATIVE_TTL_S = 10
_MISSING = b"-"


def _dump(key: ApiKey) -> bytes:
    return json.dumps(
        {
            "id": str(key.id),
            "name": key.name,
            "owner": key.owner,
            "prefix": key.prefix,
            "rpm_limit": key.rpm_limit,
            "tpd_limit": key.tpd_limit,
            "monthly_budget_usd": str(key.monthly_budget_usd)
            if key.monthly_budget_usd is not None
            else None,
            "enforce_budget": key.enforce_budget,
            "allowed_models": list(key.allowed_models) if key.allowed_models is not None else None,
            "created_at": key.created_at.isoformat(),
            "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        }
    ).encode()


def _load(raw: bytes) -> ApiKey:
    data: dict[str, Any] = json.loads(raw)
    return ApiKey(
        id=UUID(data["id"]),
        name=data["name"],
        owner=data["owner"],
        prefix=data["prefix"],
        rpm_limit=int(data["rpm_limit"]),
        tpd_limit=int(data["tpd_limit"]),
        monthly_budget_usd=Decimal(data["monthly_budget_usd"])
        if data["monthly_budget_usd"] is not None
        else None,
        enforce_budget=bool(data["enforce_budget"]),
        allowed_models=tuple(data["allowed_models"])
        if data["allowed_models"] is not None
        else None,
        created_at=datetime.fromisoformat(data["created_at"]),
        revoked_at=datetime.fromisoformat(data["revoked_at"]) if data["revoked_at"] else None,
    )


class RedisApiKeyCache:
    def __init__(self, redis: Redis, ttl_s: int, namespace: str = "relay") -> None:
        self._redis = redis
        self._ttl_s = ttl_s
        self._prefix = f"{namespace}:apikey:"

    async def get(self, key_hash: str) -> ApiKey | bool | None:
        value = await self._redis.get(self._prefix + key_hash)
        if value is None:
            return None
        raw = value if isinstance(value, bytes) else value.encode()
        if raw == _MISSING:
            return False
        return _load(raw)

    async def put(self, key_hash: str, key: ApiKey | None) -> None:
        if key is None:
            await self._redis.set(self._prefix + key_hash, _MISSING, ex=NEGATIVE_TTL_S)
        else:
            await self._redis.set(self._prefix + key_hash, _dump(key), ex=self._ttl_s)

    async def invalidate(self, key_hash: str) -> None:
        await self._redis.delete(self._prefix + key_hash)
