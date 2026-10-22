"""SQLAlchemy Core table definitions (the schema source of truth for Alembic)."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID

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
