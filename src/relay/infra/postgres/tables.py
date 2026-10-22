"""SQLAlchemy Core table definitions (the schema source of truth for Alembic)."""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

EMBEDDING_DIMENSIONS = 384

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("name", Text, nullable=False),
    Column("owner", Text, nullable=False),
    Column("key_hash", Text, nullable=False, unique=True),
    Column("prefix", Text, nullable=False),
    Column("rpm_limit", Integer, nullable=False),
    Column("tpd_limit", BigInteger, nullable=False),
    Column("monthly_budget_usd", Numeric(12, 2), nullable=True),
    Column("enforce_budget", Boolean, nullable=False, server_default="false"),
    Column("allowed_models", ARRAY(Text), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)

semantic_cache = Table(
    "semantic_cache",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("namespace", Text, nullable=False),
    Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
    Column("response", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Index("ix_semantic_cache_namespace", "namespace"),
    Index("ix_semantic_cache_expires_at", "expires_at"),
    Index(
        "ix_semantic_cache_embedding_hnsw",
        "embedding",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
)

# Append-only, one row per served request. No foreign key: inserts stay cheap and events
# outlive their keys (revoked keys keep their history anyway).
usage_events = Table(
    "usage_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("request_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("api_key_id", UUID(as_uuid=True), nullable=False),
    Column("owner", Text, nullable=False),
    Column("route", Text, nullable=False),
    Column("provider", Text, nullable=True),
    Column("provider_model", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("cache", Text, nullable=False),
    Column("stream", Boolean, nullable=False),
    Column("prompt_tokens", Integer, nullable=False),
    Column("completion_tokens", Integer, nullable=False),
    Column("cost_usd", Numeric(14, 8), nullable=False),
    Column("saved_usd", Numeric(14, 8), nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("attempts", SmallInteger, nullable=False),
    Column("replica", Text, nullable=False),
    Index("ix_usage_events_created_at", "created_at"),
    Index("ix_usage_events_api_key_id_created_at", "api_key_id", "created_at"),
)

# Written in the same transaction as the events (an upsert per day/key/route/provider), so
# dashboards read a few hundred rows instead of scanning every event.
usage_daily = Table(
    "usage_daily",
    metadata,
    Column("day", Date, nullable=False),
    Column("api_key_id", UUID(as_uuid=True), nullable=False),
    Column("route", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("requests", BigInteger, nullable=False, server_default="0"),
    Column("errors", BigInteger, nullable=False, server_default="0"),
    Column("cache_hits", BigInteger, nullable=False, server_default="0"),
    Column("semantic_hits", BigInteger, nullable=False, server_default="0"),
    Column("prompt_tokens", BigInteger, nullable=False, server_default="0"),
    Column("completion_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cost_usd", Numeric(16, 8), nullable=False, server_default="0"),
    Column("saved_usd", Numeric(16, 8), nullable=False, server_default="0"),
    Column("latency_ms_sum", BigInteger, nullable=False, server_default="0"),
    PrimaryKeyConstraint("day", "api_key_id", "route", "provider", name="pk_usage_daily"),
)

budget_alerts = Table(
    "budget_alerts",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("api_key_id", UUID(as_uuid=True), nullable=False),
    Column("key_name", Text, nullable=False),
    Column("owner", Text, nullable=False),
    Column("period", Text, nullable=False),
    Column("threshold_pct", Integer, nullable=False),
    Column("spend_usd", Numeric(14, 6), nullable=False),
    Column("budget_usd", Numeric(12, 2), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # One alert per key, period and threshold, however many replicas cross it at once.
    UniqueConstraint("api_key_id", "period", "threshold_pct", name="uq_budget_alerts_once"),
)
