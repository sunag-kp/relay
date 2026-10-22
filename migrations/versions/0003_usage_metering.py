"""usage metering and budget alerts

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("provider_model", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("cache", sa.Text(), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 8), nullable=False),
        sa.Column("saved_usd", sa.Numeric(14, 8), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("replica", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
    )
    op.create_index("ix_usage_events_created_at", "usage_events", ["created_at"])
    op.create_index(
        "ix_usage_events_api_key_id_created_at", "usage_events", ["api_key_id", "created_at"]
    )
    op.create_table(
        "usage_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("requests", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("errors", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cache_hits", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("semantic_hits", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(16, 8), server_default="0", nullable=False),
        sa.Column("saved_usd", sa.Numeric(16, 8), server_default="0", nullable=False),
        sa.Column("latency_ms_sum", sa.BigInteger(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("day", "api_key_id", "route", "provider", name="pk_usage_daily"),
    )
    op.create_table(
        "budget_alerts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_name", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("threshold_pct", sa.Integer(), nullable=False),
        sa.Column("spend_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("budget_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_budget_alerts"),
        sa.UniqueConstraint("api_key_id", "period", "threshold_pct", name="uq_budget_alerts_once"),
    )


def downgrade() -> None:
    op.drop_table("budget_alerts")
    op.drop_table("usage_daily")
    op.drop_table("usage_events")
