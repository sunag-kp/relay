from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.config import Settings
from relay.infra.postgres.db import create_engine
from relay.infra.redis.client import create_redis
from relay.mock_provider.app import Profile, create_mock_app
from tests.integration.servers import serve

CATALOG = """
providers:
  - name: mock-alpha
    kind: openai_compatible
    base_url: "${MOCK_URL}/p/alpha/v1"
    timeout_s: 5
    first_token_timeout_s: 2
  - name: mock-beta
    kind: openai_compatible
    base_url: "${MOCK_URL}/p/beta/v1"
    timeout_s: 5
    first_token_timeout_s: 2
  - name: mock-claude
    kind: anthropic
    base_url: "${MOCK_URL}/p/claude"
    timeout_s: 5
    first_token_timeout_s: 2
routes:
  - model: relay-mock
    targets:
      - {provider: mock-alpha, model: mock-large, input_per_mtok: 0.5, output_per_mtok: 1.5}
      - {provider: mock-beta, model: mock-medium, input_per_mtok: 0.2, output_per_mtok: 0.8}
  - model: relay-claude
    targets:
      - {provider: mock-claude, model: claude-haiku-5-5, input_per_mtok: 0.1, output_per_mtok: 0.5}
"""


def integration_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://relay:relay@localhost:5432/relay"
        ),
        "redis_url": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "replica_id": "it",
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
def settings() -> Settings:
    return integration_settings()


@pytest.fixture(scope="session")
async def mock_url() -> AsyncIterator[str]:
    app = create_mock_app(
        {
            "default": Profile(latency_ms=5, jitter_ms=0),
            "alpha": Profile(latency_ms=5, jitter_ms=0, stream_chunk_delay_ms=20),
            "beta": Profile(latency_ms=5, jitter_ms=0),
            "claude": Profile(latency_ms=5, jitter_ms=0),
        }
    )
    async with serve(app, lifespan="off") as url:
        yield url


@pytest.fixture
def catalog_file(tmp_path: Path, mock_url: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MOCK_URL", mock_url)
    path = tmp_path / "relay.yaml"
    path.write_text(CATALOG)
    return path


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings)
    yield engine
    await engine.dispose()


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    client = create_redis(settings.redis_url)
    yield client
    await client.aclose()


@pytest.fixture
async def clean_db(engine: AsyncEngine, redis: Redis) -> AsyncEngine:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE api_keys, semantic_cache"))
    async for key in redis.scan_iter(match="relay:cache:*", count=500):
        await redis.unlink(key)
    return engine
