from __future__ import annotations

import os

import pytest

from relay.config import Settings


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
