"""Process configuration, read from environment variables (and `.env` in development).

Everything that differs between laptop, CI and production is an environment variable; the
provider/route catalog lives in a YAML file whose path is itself an environment variable.
"""

from __future__ import annotations

import socket
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_ADMIN_TOKEN = "dev-admin-token"  # noqa: S105 - development default, refused in prod


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------------------------------------------------------------------ process
    app_env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "relay-gateway"
    replica_id: str = Field(default_factory=socket.gethostname)
    host: str = "0.0.0.0"  # noqa: S104 - containers must bind all interfaces
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = True

    # ------------------------------------------------------------------ storage
    database_url: str = "postgresql+asyncpg://relay:relay@localhost:5432/relay"
    database_pool_size: int = 10
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 100

    # ------------------------------------------------------------------ auth
    admin_token: SecretStr = SecretStr(DEV_ADMIN_TOKEN)
    # Serve the read-only stats endpoints (used by the dashboard) without the admin token.
    # Key management always needs the admin token.
    dashboard_public: bool = False
    api_key_cache_ttl_s: int = 60

    # ------------------------------------------------------------------ routing
    relay_config_file: Path = Path("config/relay.yaml")

    # ------------------------------------------------------------------ limits
    default_rpm_limit: int = 60
    default_tpd_limit: int = 1_000_000
    # Tokens reserved for the completion when a request does not set max_tokens; the
    # reservation is settled against real usage once the provider answers.
    default_completion_reserve_tokens: int = 256

    # ------------------------------------------------------------------ caching
    cache_exact_enabled: bool = True
    cache_semantic_enabled: bool = True
    cache_ttl_s: int = 3600
    semantic_cache_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    embedding_provider: Literal["hashing", "fastembed"] = "hashing"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: Path | None = None

    # ------------------------------------------------------------------ metering
    usage_flush_interval_s: float = 1.0
    usage_batch_size: int = 500
    usage_queue_max: int = 50_000
    budget_alert_webhook_url: str | None = None

    # ------------------------------------------------------------------ telemetry
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_traces_sampler_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    provider_health_publish_interval_s: float = 5.0

    # ------------------------------------------------------------------ demo / packaging
    # Mount the mock provider inside the gateway process (single-container demo deploys).
    mount_mock_provider: bool = False
    # If set, this exact key is provisioned at startup (idempotently) for the public demo.
    demo_api_key: SecretStr | None = None
    demo_rpm_limit: int = 20
    demo_tpd_limit: int = 200_000
    ui_dist_dir: Path = Path("ui/dist")

    @model_validator(mode="after")
    def _refuse_dev_secrets_in_prod(self) -> Settings:
        if self.app_env == "prod" and self.admin_token.get_secret_value() == DEV_ADMIN_TOKEN:
            raise ValueError("ADMIN_TOKEN must be set to a real secret when APP_ENV=prod")
        return self

    @property
    def async_database_url(self) -> str:
        return normalize_database_url(self.database_url)

    @property
    def database_uses_pgbouncer(self) -> bool:
        # Neon's pooled endpoints (`-pooler` hosts) run PgBouncer in transaction mode, which
        # breaks asyncpg's server-side prepared statement cache.
        return "-pooler" in (urlsplit(self.database_url).hostname or "")


def normalize_database_url(url: str) -> str:
    """Accept the URL a hosting dashboard hands out and turn it into an asyncpg URL.

    Neon gives `postgresql://...?sslmode=require&channel_binding=require`; asyncpg wants the
    `postgresql+asyncpg` scheme and `ssl=require`, and does not know `channel_binding`.
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in {"postgres", "postgresql"}:
        scheme = "postgresql+asyncpg"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            query.append(("ssl", value))
        elif key == "channel_binding":
            continue
        else:
            query.append((key, value))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
