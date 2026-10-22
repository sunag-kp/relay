"""Composition root: builds every long-lived object once per process and wires ports to
adapters. Nothing else in the code base constructs infrastructure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from relay.infra.postgres.usage import PostgresAlertStore, PostgresUsageStore
from relay.infra.providers.registry import build_providers
from relay.infra.redis.client import create_redis
from relay.infra.redis.exact_cache import RedisExactCache
from relay.infra.redis.health_board import RedisHealthBoard
from relay.infra.redis.key_cache import RedisApiKeyCache
from relay.infra.redis.rate_limiter import RedisRateLimiter
from relay.infra.webhook import WebhookNotifier
from relay.services.alerts import BudgetAlerts
from relay.services.background import BackgroundTasks, PeriodicTasks
from relay.services.cache import ResponseCache
from relay.services.chat import ChatService
from relay.services.keys import KeyService
from relay.services.limits import QuotaService
from relay.services.ports import ChatProvider, Embedder, HealthBoard
from relay.services.resilience import ProviderHealth, ResilientExecutor
from relay.services.usage import UsageRecorder, UsageReports

ReadinessCheck = Callable[[], Awaitable[Any]]

# From benchmarks/semantic_threshold.py (results in benchmarks/results/semantic-threshold/).
# hashing: a margin above the worst different-intent pair (0.83) -> zero false positives.
# fastembed (bge-small): no threshold was free of false positives (worst pair 0.995), so the
# default is strict; dense embedders are an opt-in. openai: not measured, same strict default.
DEFAULT_SEMANTIC_THRESHOLDS = {"hashing": 0.88, "fastembed": 0.97, "openai": 0.97}


@dataclass
class Container:
    settings: Settings
    catalog: Catalog
    providers: Mapping[str, ChatProvider]
    chat: ChatService
    keys: KeyService
    usage: UsageRecorder | None = None
    reports: UsageReports | None = None
    alerts: BudgetAlerts | None = None
    health_board: HealthBoard | None = None
    background: BackgroundTasks = field(default_factory=BackgroundTasks)
    periodic: PeriodicTasks = field(default_factory=PeriodicTasks)
    redis: Redis | None = None
    engine: AsyncEngine | None = None
    readiness_checks: dict[str, ReadinessCheck] = field(default_factory=dict)

    def start(self) -> None:
        if self.usage is not None:
            self.usage.start()
        self.periodic.start()

    async def stop(self) -> None:
        """Stop background work and flush buffers (graceful shutdown)."""
        await self.periodic.stop()
        await self.background.drain()
        if self.usage is not None:
            await self.usage.stop()

    async def aclose(self) -> None:
        await self.stop()
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
    usage_store = PostgresUsageStore(engine)
    usage = UsageRecorder(
        usage_store,
        batch_size=settings.usage_batch_size,
        flush_interval_s=settings.usage_flush_interval_s,
        max_queue=settings.usage_queue_max,
    )
    notifier = (
        WebhookNotifier(settings.budget_alert_webhook_url)
        if settings.budget_alert_webhook_url
        else None
    )
    alerts = BudgetAlerts(PostgresAlertStore(engine), notifier)
    health_board = RedisHealthBoard(redis)

    periodic = PeriodicTasks()
    if cache is not None:
        periodic.every(settings.cache_purge_interval_s, cache.purge_expired, name="cache-purge")

    async def publish_health() -> None:
        interval = settings.provider_health_publish_interval_s
        await health_board.publish(
            settings.replica_id, health.snapshot(), ttl_s=max(3, int(interval * 3))
        )

    async def purge_usage() -> None:
        cutoff = datetime.now(UTC) - timedelta(days=settings.usage_retention_days)
        await usage_store.purge_before(cutoff)

    periodic.every(settings.provider_health_publish_interval_s, publish_health, name="health")
    periodic.every(3600, purge_usage, name="usage-retention")
    chat = ChatService(
        catalog,
        providers,
        health,
        ResilientExecutor(catalog, providers, health),
        quotas,
        background,
        cache=cache,
        usage=usage,
        alerts=alerts,
        replica=settings.replica_id,
        request_timeout_s=settings.request_timeout_s,
        default_completion_tokens=settings.default_completion_reserve_tokens,
    )
    return Container(
        settings=settings,
        catalog=catalog,
        providers=providers,
        chat=chat,
        usage=usage,
        reports=UsageReports(usage_store),
        alerts=alerts,
        health_board=health_board,
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
