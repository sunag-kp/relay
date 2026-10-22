from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.domain.usage import BudgetAlert, DailyRollup, UsageEvent
from relay.infra.postgres.tables import budget_alerts, usage_daily, usage_events
from relay.services.ports import DailyUsageRow

_SUMMED = (
    "requests",
    "errors",
    "cache_hits",
    "semantic_hits",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "saved_usd",
    "latency_ms_sum",
)


class PostgresUsageStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def write(self, events: Sequence[UsageEvent], rollups: Sequence[DailyRollup]) -> None:
        upsert = pg_insert(usage_daily)
        upsert = upsert.on_conflict_do_update(
            constraint="pk_usage_daily",
            # Additive upsert: concurrent writers from every replica each add their batch.
            set_={column: usage_daily.c[column] + upsert.excluded[column] for column in _SUMMED},
        )
        async with self._engine.begin() as conn:
            await conn.execute(insert(usage_events), [_event_row(e) for e in events])
            await conn.execute(upsert, [_rollup_row(r) for r in rollups])

    async def daily(
        self, since: date, until: date, api_key_id: UUID | None = None
    ) -> list[DailyUsageRow]:
        query = select(usage_daily).where(usage_daily.c.day.between(since, until))
        if api_key_id is not None:
            query = query.where(usage_daily.c.api_key_id == api_key_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [DailyUsageRow(**row._mapping) for row in rows]

    async def recent(self, limit: int, api_key_id: UUID | None = None) -> list[UsageEvent]:
        query = select(usage_events).order_by(usage_events.c.id.desc()).limit(limit)
        if api_key_id is not None:
            query = query.where(usage_events.c.api_key_id == api_key_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [UsageEvent(**{k: v for k, v in row._mapping.items() if k != "id"}) for row in rows]

    async def purge_before(self, cutoff: datetime) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(usage_events).where(usage_events.c.created_at < cutoff)
            )
        return int(result.rowcount or 0)


def _event_row(event: UsageEvent) -> dict[str, Any]:
    return {
        "request_id": event.request_id,
        "created_at": event.created_at,
        "api_key_id": event.api_key_id,
        "owner": event.owner,
        "route": event.route,
        "provider": event.provider,
        "provider_model": event.provider_model,
        "status": event.status,
        "cache": event.cache,
        "stream": event.stream,
        "prompt_tokens": event.prompt_tokens,
        "completion_tokens": event.completion_tokens,
        "cost_usd": event.cost_usd,
        "saved_usd": event.saved_usd,
        "latency_ms": event.latency_ms,
        "attempts": event.attempts,
        "replica": event.replica,
    }


def _rollup_row(rollup: DailyRollup) -> dict[str, Any]:
    return {
        "day": rollup.day,
        "api_key_id": rollup.api_key_id,
        "route": rollup.route,
        "provider": rollup.provider,
        **{column: getattr(rollup, column) for column in _SUMMED},
    }


class PostgresAlertStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert_if_new(self, alert: BudgetAlert) -> bool:
        statement = (
            pg_insert(budget_alerts)
            .values(
                api_key_id=alert.api_key_id,
                key_name=alert.key_name,
                owner=alert.owner,
                period=alert.period,
                threshold_pct=alert.threshold_pct,
                spend_usd=alert.spend_usd,
                budget_usd=alert.budget_usd,
                created_at=alert.created_at,
            )
            .on_conflict_do_nothing(constraint="uq_budget_alerts_once")
            .returning(budget_alerts.c.id)
        )
        async with self._engine.begin() as conn:
            inserted = (await conn.execute(statement)).first()
        return inserted is not None

    async def recent(self, limit: int) -> list[BudgetAlert]:
        query = select(budget_alerts).order_by(budget_alerts.c.id.desc()).limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [BudgetAlert(**{k: v for k, v in row._mapping.items() if k != "id"}) for row in rows]
