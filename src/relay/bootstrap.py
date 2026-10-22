"""Composition root: builds every long-lived object once per process and wires ports to
adapters. Nothing else in the code base constructs infrastructure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.config import Settings
from relay.infra.postgres.db import create_engine, ping
from relay.infra.redis.client import create_redis

ReadinessCheck = Callable[[], Awaitable[Any]]


@dataclass
class Container:
    settings: Settings
    redis: Redis
    engine: AsyncEngine
    readiness_checks: dict[str, ReadinessCheck] = field(default_factory=dict)

    async def aclose(self) -> None:
        await self.redis.aclose()
        await self.engine.dispose()


async def build_container(settings: Settings) -> Container:
    redis = create_redis(settings.redis_url, max_connections=settings.redis_max_connections)
    engine = create_engine(settings)

    async def redis_ready() -> None:
        await redis.ping()

    async def postgres_ready() -> None:
        await ping(engine)

    return Container(
        settings=settings,
        redis=redis,
        engine=engine,
        readiness_checks={"redis": redis_ready, "postgres": postgres_ready},
    )
