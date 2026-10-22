"""Usage metering: one event per served request, rolled up per day."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class UsageEvent:
    request_id: str
    api_key_id: UUID
    owner: str
    route: str
    provider: str | None
    provider_model: str | None
    status: str  # "ok" or the error code
    cache: str  # miss | exact | semantic | bypass
    stream: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    saved_usd: Decimal = Decimal(0)  # provider spend avoided by a cache hit
    latency_ms: int = 0
    attempts: int = 0
    replica: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def day(self) -> date:
        return self.created_at.date()


@dataclass(slots=True)
class DailyRollup:
    """Usage for one (day, key, route, provider), summed in memory before one upsert."""

    day: date
    api_key_id: UUID
    route: str
    provider: str
    requests: int = 0
    errors: int = 0
    cache_hits: int = 0
    semantic_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    saved_usd: Decimal = Decimal(0)
    latency_ms_sum: int = 0

    def add(self, event: UsageEvent) -> None:
        self.requests += 1
        self.errors += event.status != "ok"
        self.cache_hits += event.cache in {"exact", "semantic"}
        self.semantic_hits += event.cache == "semantic"
        self.prompt_tokens += event.prompt_tokens
        self.completion_tokens += event.completion_tokens
        self.cost_usd += event.cost_usd
        self.saved_usd += event.saved_usd
        self.latency_ms_sum += event.latency_ms


def roll_up(events: list[UsageEvent]) -> list[DailyRollup]:
    rollups: dict[tuple[date, UUID, str, str], DailyRollup] = {}
    for event in events:
        provider = event.provider or "-"
        key = (event.day, event.api_key_id, event.route, provider)
        if key not in rollups:
            rollups[key] = DailyRollup(event.day, event.api_key_id, event.route, provider)
        rollups[key].add(event)
    return list(rollups.values())


@dataclass(frozen=True, slots=True)
class BudgetAlert:
    api_key_id: UUID
    key_name: str
    owner: str
    period: str
    threshold_pct: int
    spend_usd: Decimal
    budget_usd: Decimal
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
