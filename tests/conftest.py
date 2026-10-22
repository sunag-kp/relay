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
from relay.services.chat import ChatService
from relay.services.keys import KeyService, KeySpec
from tests.fakes import FakeKeyCache, FakeKeyRepository, FakeProvider


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


@dataclass
class Harness:
    container: Container
    providers: dict[str, FakeProvider]
    client: httpx.AsyncClient
    api_key: str
    repository: FakeKeyRepository
    extra: dict[str, Any] = field(default_factory=dict)

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
    ) -> Harness:
        catalog = catalog or make_catalog()
        providers = providers or {p.name: FakeProvider(p.name) for p in catalog.providers}
        settings = settings or Settings(replica_id="test", app_env="test")
        repository = FakeKeyRepository()
        keys = KeyService(repository, FakeKeyCache())
        _, plaintext = await keys.create(
            KeySpec(name="test", owner="team-a", rpm_limit=1000, tpd_limit=10**9)
        )
        container = Container(
            settings=settings,
            catalog=catalog,
            providers=providers,
            chat=ChatService(catalog, providers),
            keys=keys,
        )
        app = create_app(container=container)
        manager = LifespanManager(app)
        await manager.__aenter__()
        managers.append(manager)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url="http://test"
        )
        clients.append(client)
        return Harness(container, providers, client, plaintext, repository)

    yield factory
    for client in clients:
        await client.aclose()
    for manager in managers:
        await manager.__aexit__(None, None, None)
