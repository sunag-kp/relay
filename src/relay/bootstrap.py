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
from relay.infra.embeddings import FastEmbedEmbedder, HashingEmbedder, OpenAIEmbedder
from relay.infra.postgres.db import create_engine, ping
from relay.infra.postgres.keys import PostgresApiKeyRepository
from relay.infra.postgres.semantic_cache import PostgresSemanticCache
from relay.infra.providers.registry import build_providers
from relay.infra.redis.client import create_redis
from relay.infra.redis.exact_cache import RedisExactCache
from relay.infra.redis.key_cache import RedisApiKeyCache
from relay.infra.redis.rate_limiter import RedisRateLimiter
from relay.services.background import BackgroundTasks, PeriodicTasks
from relay.services.cache import ResponseCache
from relay.services.chat import ChatService
from relay.services.keys import KeyService
from relay.services.limits import QuotaService
from relay.services.ports import ChatProvider, Embedder
from relay.services.resilience import ProviderHealth, ResilientExecutor

ReadinessCheck = Callable[[], Awaitable[Any]]

# Calibrated with benchmarks/semantic_threshold.py: a margin above the highest similarity of
# any different-intent pair in the labelled set (results under benchmarks/results/).
DEFAULT_SEMANTIC_THRESHOLDS = {"hashing": 0.88, "fastembed": 0.90, "openai": 0.90}


@dataclass
class Container:
    settings: Settings
    catalog: Catalog
    providers: Mapping[str, ChatProvider]
    chat: ChatService
    keys: KeyService
    background: BackgroundTasks = field(default_factory=BackgroundTasks)
    periodic: PeriodicTasks = field(default_factory=PeriodicTasks)
    redis: Redis | None = None
    engine: AsyncEngine | None = None
    readiness_checks: dict[str, ReadinessCheck] = field(default_factory=dict)

    async def aclose(self) -> None:
        await self.periodic.stop()
        await self.background.drain()
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
    background = BackgroundTasks()
    quotas = QuotaService(
        RedisRateLimiter(redis),
        default_completion_tokens=settings.default_completion_reserve_tokens,
        fail_open=settings.rate_limit_fail_open,
    )
    cache = build_cache(settings, redis, engine)
    periodic = PeriodicTasks()
    if cache is not None:
        periodic.every(settings.cache_purge_interval_s, cache.purge_expired, name="cache-purge")
    chat = ChatService(
        catalog,
        providers,
        health,
        ResilientExecutor(catalog, providers, health),
        quotas,
        background,
        cache=cache,
        request_timeout_s=settings.request_timeout_s,
        default_completion_tokens=settings.default_completion_reserve_tokens,
    )
    return Container(
        settings=settings,
        catalog=catalog,
        providers=providers,
        chat=chat,
        background=background,
        periodic=periodic,
        keys=KeyService(
            PostgresApiKeyRepository(engine),
            RedisApiKeyCache(redis, ttl_s=settings.api_key_cache_ttl_s),
        ),
        redis=redis,
        engine=engine,
        readiness_checks={"redis": redis_ready, "postgres": postgres_ready},
    )


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "fastembed":
        return FastEmbedEmbedder(settings.embedding_model, settings.embedding_cache_dir)
    if settings.embedding_provider == "openai":
        return OpenAIEmbedder()
    return HashingEmbedder()


def build_cache(settings: Settings, redis: Redis, engine: AsyncEngine) -> ResponseCache | None:
    if not (settings.cache_exact_enabled or settings.cache_semantic_enabled):
        return None
    semantic = settings.cache_semantic_enabled
    return ResponseCache(
        RedisExactCache(redis) if settings.cache_exact_enabled else None,
        PostgresSemanticCache(engine) if semantic else None,
        build_embedder(settings) if semantic else None,
        ttl_s=settings.cache_ttl_s,
        threshold=settings.semantic_cache_threshold
        or DEFAULT_SEMANTIC_THRESHOLDS[settings.embedding_provider],
    )
