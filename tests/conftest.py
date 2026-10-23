from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from relay.api.app import create_app
from relay.bootstrap import Container
from relay.config import Settings
from relay.domain.catalog import Catalog
from relay.infra.embeddings import HashingEmbedder
from relay.mock_provider.app import Profile, create_mock_app
from relay.services.alerts import BudgetAlerts
from relay.services.background import BackgroundTasks
from relay.services.cache import ResponseCache
from relay.services.chat import ChatService
from relay.services.keys import KeyService, KeySpec
from relay.services.limits import QuotaService
from relay.services.resilience import ProviderHealth, ResilientExecutor
from relay.services.usage import UsageRecorder, UsageReports
from tests.fakes import (
    FakeAlertStore,
    FakeExactCache,
    FakeHealthBoard,
    FakeKeyCache,
    FakeKeyRepository,
    FakeNotifier,
    FakeProvider,
    FakeRateLimiter,
    FakeSemanticCache,
    FakeUsageStore,
)
from tests.servers import StubUpstream, serve


def make_catalog(**route_overrides: Any) -> Catalog:
    route: dict[str, Any] = {
        "model": "test-model",
        "policy": "priority",
        "targets": [
            {"provider": "alpha", "model": "alpha-1", "input_per_mtok": 1, "output_per_mtok": 2},
            {"provider": "beta", "model": "beta-1", "input_per_mtok": 0.5, "output_per_mtok": 1},
        ],
    }
    route.update(route_overrides)
    return Catalog.model_validate(
        {
            "providers": [
                {"name": "alpha", "kind": "openai_compatible", "base_url": "http://alpha"},
                {"name": "beta", "kind": "openai_compatible", "base_url": "http://beta"},
            ],
            "routes": [route],
        }
    )


DEFAULT_KEY = KeySpec(name="test", owner="team-a", rpm_limit=1000, tpd_limit=10**9)


async def _no_sleep(_seconds: float) -> None:
    return None


@dataclass
class Harness:
    container: Container
    providers: dict[str, FakeProvider]
    client: httpx.AsyncClient
    api_key: str
    repository: FakeKeyRepository
    limiter: FakeRateLimiter
    usage_store: FakeUsageStore
    alert_store: FakeAlertStore
    notifier: FakeNotifier
    extra: dict[str, Any] = field(default_factory=dict)

    async def drain(self) -> None:
        """Let background work (settlement, alerts, cache writes) finish, then write usage."""
        await self.container.background.drain()
        assert self.container.usage is not None
        await self.container.usage.flush()

    @property
    def admin(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.container.settings.admin_token.get_secret_value()}"}

    @property
    def auth(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.api_key}"}

    async def chat(self, **body: Any) -> httpx.Response:
        payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}], **body}
        return await self.client.post("/v1/chat/completions", json=payload, headers=self.auth)


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest.fixture
async def make_harness() -> AsyncIterator[HarnessFactory]:
    managers: list[LifespanManager] = []
    clients: list[httpx.AsyncClient] = []

    async def factory(
        catalog: Catalog | None = None,
        providers: dict[str, FakeProvider] | None = None,
        settings: Settings | None = None,
        key_spec: KeySpec | None = None,
        cache: bool = False,
        threshold: float = 0.9,
    ) -> Harness:
        catalog = catalog or make_catalog()
        providers = providers or {p.name: FakeProvider(p.name) for p in catalog.providers}
        settings = settings or Settings(replica_id="test", app_env="test")
        repository = FakeKeyRepository()
        keys = KeyService(repository, FakeKeyCache())
        _, plaintext = await keys.create(key_spec or DEFAULT_KEY)
        health = ProviderHealth(catalog)
        executor = ResilientExecutor(catalog, providers, health, sleep=_no_sleep)
        limiter = FakeRateLimiter()
        background = BackgroundTasks()
        quotas = QuotaService(limiter, fail_open=settings.rate_limit_fail_open)
        usage_store, alert_store, notifier = FakeUsageStore(), FakeAlertStore(), FakeNotifier()
        usage = UsageRecorder(usage_store, flush_interval_s=3600)
        alerts = BudgetAlerts(alert_store, notifier)
        container = Container(
            settings=settings,
            catalog=catalog,
            providers=providers,
            chat=ChatService(
                catalog,
                providers,
                health,
                executor,
                quotas,
                background,
                usage=usage,
                alerts=alerts,
                replica=settings.replica_id,
                cache=ResponseCache(
                    FakeExactCache(),
                    FakeSemanticCache(),
                    HashingEmbedder(),
                    ttl_s=60,
                    threshold=threshold,
                )
                if cache
                else None,
            ),
            background=background,
            keys=keys,
            usage=usage,
            reports=UsageReports(usage_store),
            alerts=alerts,
            health_board=FakeHealthBoard(),
        )
        app = create_app(container=container)
        manager = LifespanManager(app)
        await manager.__aenter__()
        managers.append(manager)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url="http://test"
        )
        clients.append(client)
        return Harness(
            container,
            providers,
            client,
            plaintext,
            repository,
            limiter,
            usage_store,
            alert_store,
            notifier,
        )

    yield factory
    for client in clients:
        await client.aclose()
    for manager in managers:
        await manager.__aexit__(None, None, None)


@pytest.fixture(scope="session")
async def mock_provider_url() -> AsyncIterator[str]:
    """The mock provider on a real local socket, with no artificial latency."""
    fast = Profile(latency_ms=0, jitter_ms=0, stream_chunk_delay_ms=0)
    app = create_mock_app({"default": fast, "alpha": fast, "claude": fast})
    async with serve(app, lifespan="off") as url:
        yield url


@pytest.fixture(scope="session")
async def stub_upstream() -> AsyncIterator[tuple[str, StubUpstream]]:
    stub = StubUpstream()
    async with serve(stub.app(), lifespan="off") as url:
        yield url, stub
