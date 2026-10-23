from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from relay.config import Settings, normalize_database_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "postgresql://u:p@ep-cool-1.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-cool-1.eu-central-1.aws.neon.tech/neondb?ssl=require",
        ),
        ("postgres://u:p@db:5432/relay", "postgresql+asyncpg://u:p@db:5432/relay"),
        (
            "postgresql+asyncpg://relay:relay@localhost:5432/relay",
            "postgresql+asyncpg://relay:relay@localhost:5432/relay",
        ),
    ],
)
def test_database_url_is_normalized_for_asyncpg(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected


def test_pooled_neon_endpoint_is_detected() -> None:
    settings = Settings(database_url="postgresql://u:p@ep-x-pooler.neon.tech/db")
    assert settings.database_uses_pgbouncer
    assert not Settings(database_url="postgresql://u:p@ep-x.neon.tech/db").database_uses_pgbouncer


def test_prod_refuses_the_development_admin_token() -> None:
    with pytest.raises(ValidationError, match="ADMIN_TOKEN"):
        Settings(app_env="prod")
    assert Settings(app_env="prod", admin_token=SecretStr("s3cret")).app_env == "prod"


def test_demo_allowed_models_parse_from_a_comma_separated_list() -> None:
    assert Settings().demo_models is None
    settings = Settings(demo_allowed_models=" relay-mock, relay-mock-cheapest ,")
    assert settings.demo_models == ("relay-mock", "relay-mock-cheapest")
