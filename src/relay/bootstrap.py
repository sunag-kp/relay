"""Composition root: builds every long-lived object once per process and wires ports to
adapters. Nothing else in the code base constructs infrastructure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.config import Settings
from relay.domain.catalog import Catalog
from relay.infra.catalog_loader import load_catalog
from relay.infra.postgres.db import create_engine, ping
from relay.infra.postgres.keys import PostgresApiKeyRepository
from relay.infra.providers.registry import build_providers
from relay.infra.redis.client import create_redis
from relay.infra.redis.key_cache import RedisApiKeyCache
from relay.services.chat import ChatService
from relay.services.keys import KeyService
from relay.services.ports import ChatProvider
from relay.services.resilience import ProviderHealth, ResilientExecutor

ReadinessCheck = Callable[[], Awaitable[Any]]


@dataclass
class Container:
    settings: Settings
    catalog: Catalog
    providers: Mapping[str, ChatProvider]
    chat: ChatService
    keys: KeyService
    redis: Redis | None = None
    engine: AsyncEngine | None = None
    readiness_checks: dict[str, ReadinessCheck] = field(default_factory=dict)

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()
        if self.redis is not None:
            await self.redis.aclose()
        if self.engine is not None:
            await self.engine.dispose()


async def build_container(settings: Settings) -> Container:
    catalog = load_catalog(settings.relay_config_file)
    providers = build_providers(catalog)
    redis = create_redis(settings.redis_url, max_connections=settings.redis_max_connections)
    engine = create_engine(settings)

    async def redis_ready() -> None:
        await redis.ping()

    async def postgres_ready() -> None:
        await ping(engine)

    health = ProviderHealth(catalog)
    chat = ChatService(
        catalog,
        providers,
        health,
        ResilientExecutor(catalog, providers, health),
        request_timeout_s=settings.request_timeout_s,
        default_completion_tokens=settings.default_completion_reserve_tokens,
    )
    return Container(
        settings=settings,
        catalog=catalog,
        providers=providers,
        chat=chat,
        keys=KeyService(
            PostgresApiKeyRepository(engine),
            RedisApiKeyCache(redis, ttl_s=settings.api_key_cache_ttl_s),
        ),
        redis=redis,
        engine=engine,
        readiness_checks={"redis": redis_ready, "postgres": postgres_ready},
    )
