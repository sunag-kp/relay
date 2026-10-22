"""Ports: the interfaces the services layer needs from the outside world.

Infrastructure adapters (`relay.infra`) implement these; tests substitute in-memory fakes.
Services never import an adapter, which `import-linter` enforces in CI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from relay.domain.cache import CachedCompletion
from relay.domain.chat import ChatCompletionRequest, Completion
from relay.domain.keys import ApiKey
from relay.domain.limits import Admission, LimitCheck
from relay.domain.usage import BudgetAlert, DailyRollup, UsageEvent

StreamEvent = dict[str, Any]


class ChatProvider(Protocol):
    """One upstream LLM API, spoken to in OpenAI wire format."""

    @property
    def name(self) -> str: ...

    @property
    def configured(self) -> bool:
        """False when the provider needs credentials that are not set in this deployment."""
        ...

    def supports(self, request: ChatCompletionRequest) -> bool:
        """Whether this provider can serve the request at all (e.g. tool calls)."""
        ...

    async def complete(self, request: ChatCompletionRequest, model: str) -> Completion: ...

    def stream(self, request: ChatCompletionRequest, model: str) -> AsyncIterator[StreamEvent]:
        """Yield `chat.completion.chunk` dicts. The last chunk carries `usage` when the
        provider reports it. Raises `ProviderFailure` for any upstream error."""
        ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------- API keys


@dataclass(frozen=True, slots=True)
class KeyChanges:
    name: str | None = None
    rpm_limit: int | None = None
    tpd_limit: int | None = None
    monthly_budget_usd: float | None = None
    clear_budget: bool = False
    enforce_budget: bool | None = None
    allowed_models: list[str] | None = None
    clear_allowed_models: bool = False


class ApiKeyRepository(Protocol):
    async def get_by_hash(self, key_hash: str) -> ApiKey | None: ...

    async def get(self, key_id: UUID) -> ApiKey | None: ...

    async def insert(self, key: ApiKey, key_hash: str) -> None:
        """Raises `Conflict` if a key with the same hash exists."""
        ...

    async def list(self, *, include_revoked: bool) -> list[ApiKey]: ...

    async def update(self, key_id: UUID, changes: KeyChanges) -> ApiKey | None: ...

    async def revoke(self, key_id: UUID) -> ApiKey | None: ...

    async def hash_of(self, key_id: UUID) -> str | None: ...


class ApiKeyCache(Protocol):
    """A shared, short-TTL cache of key records so the hot path skips Postgres."""

    async def get(self, key_hash: str) -> ApiKey | bool | None:
        """The cached key, `False` for a cached "no such key", or `None` on a miss."""
        ...

    async def put(self, key_hash: str, key: ApiKey | None) -> None: ...

    async def invalidate(self, key_hash: str) -> None: ...


# ---------------------------------------------------------------- limits


class RateLimiter(Protocol):
    """Atomic, distributed per-key limits. Raises `DependencyUnavailable` when the backing
    store is down."""

    async def admit(self, check: LimitCheck) -> Admission: ...

    async def settle(
        self, check: LimitCheck, actual_tokens: int, spend_micro_usd: int
    ) -> tuple[int, int]:
        """Replace the reserved tokens with the actual count and add spend.
        Returns (remaining tokens, total spend this period in micro-USD)."""
        ...


# ---------------------------------------------------------------- caching


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    entry: CachedCompletion
    similarity: float


class ExactCache(Protocol):
    async def get(self, key: str) -> CachedCompletion | None: ...

    async def put(self, key: str, entry: CachedCompletion, ttl_s: int) -> None: ...

    async def flush(self) -> int: ...


class SemanticCache(Protocol):
    async def nearest(self, namespace: str, embedding: Sequence[float]) -> SemanticMatch | None: ...

    async def put(
        self, namespace: str, embedding: Sequence[float], entry: CachedCompletion, ttl_s: int
    ) -> None: ...

    async def purge_expired(self) -> int: ...

    async def flush(self) -> int: ...


class Embedder(Protocol):
    @property
    def id(self) -> str:
        """Identifies the model; part of the semantic namespace, so changing the embedder
        never compares vectors from two different models."""
        ...

    async def embed(self, text: str) -> list[float]: ...


# ---------------------------------------------------------------- metering


@dataclass(frozen=True, slots=True)
class DailyUsageRow:
    day: date
    api_key_id: UUID
    route: str
    provider: str
    requests: int
    errors: int
    cache_hits: int
    semantic_hits: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    saved_usd: Decimal
    latency_ms_sum: int


class UsageStore(Protocol):
    async def write(self, events: Sequence[UsageEvent], rollups: Sequence[DailyRollup]) -> None:
        """Insert the events and add the rollups to the daily totals, atomically."""
        ...

    async def daily(
        self, since: date, until: date, api_key_id: UUID | None = None
    ) -> list[DailyUsageRow]: ...

    async def recent(self, limit: int, api_key_id: UUID | None = None) -> list[UsageEvent]: ...

    async def purge_before(self, cutoff: datetime) -> int: ...


class AlertStore(Protocol):
    async def insert_if_new(self, alert: BudgetAlert) -> bool:
        """False if this key/period/threshold already alerted (from any replica)."""
        ...

    async def recent(self, limit: int) -> list[BudgetAlert]: ...


class AlertNotifier(Protocol):
    async def notify(self, alert: BudgetAlert) -> None: ...


class HealthBoard(Protocol):
    """Where replicas publish their view of provider health, for the dashboard."""

    async def publish(self, replica: str, snapshot: dict[str, Any], ttl_s: int) -> None: ...

    async def read_all(self) -> dict[str, dict[str, Any]]: ...
