"""In-memory fakes for the service ports, used by unit tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from relay.domain.cache import CachedCompletion
from relay.domain.chat import (
    ChatCompletionRequest,
    Completion,
    Usage,
    chunk,
    new_completion_id,
    now_ts,
)
from relay.domain.errors import Conflict, DependencyUnavailable, FailureKind, ProviderFailure
from relay.domain.keys import ApiKey
from relay.domain.limits import Admission, LimitCheck, LimitReason
from relay.domain.usage import BudgetAlert, DailyRollup, UsageEvent
from relay.infra.embeddings import cosine
from relay.services.ports import DailyUsageRow, KeyChanges, SemanticMatch, StreamEvent


class FakeKeyRepository:
    def __init__(self) -> None:
        self.keys: dict[UUID, ApiKey] = {}
        self.hashes: dict[UUID, str] = {}
        self.lookups = 0

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        self.lookups += 1
        for key_id, stored in self.hashes.items():
            if stored == key_hash:
                return self.keys[key_id]
        return None

    async def get(self, key_id: UUID) -> ApiKey | None:
        return self.keys.get(key_id)

    async def insert(self, key: ApiKey, key_hash: str) -> None:
        if key_hash in self.hashes.values():
            raise Conflict("duplicate")
        self.keys[key.id] = key
        self.hashes[key.id] = key_hash

    async def list(self, *, include_revoked: bool) -> list[ApiKey]:
        return [k for k in self.keys.values() if include_revoked or k.active]

    async def update(self, key_id: UUID, changes: KeyChanges) -> ApiKey | None:
        key = self.keys.get(key_id)
        if key is None:
            return None
        values: dict[str, Any] = {}
        if changes.name is not None:
            values["name"] = changes.name
        if changes.rpm_limit is not None:
            values["rpm_limit"] = changes.rpm_limit
        if changes.tpd_limit is not None:
            values["tpd_limit"] = changes.tpd_limit
        if changes.clear_budget:
            values["monthly_budget_usd"] = None
        elif changes.monthly_budget_usd is not None:
            values["monthly_budget_usd"] = Decimal(str(changes.monthly_budget_usd))
        if changes.enforce_budget is not None:
            values["enforce_budget"] = changes.enforce_budget
        if changes.clear_allowed_models:
            values["allowed_models"] = None
        elif changes.allowed_models is not None:
            values["allowed_models"] = tuple(changes.allowed_models)
        self.keys[key_id] = replace(key, **values)
        return self.keys[key_id]

    async def revoke(self, key_id: UUID) -> ApiKey | None:
        key = self.keys.get(key_id)
        if key is None:
            return None
        if key.revoked_at is None:
            self.keys[key_id] = replace(key, revoked_at=datetime.now(UTC))
        return self.keys[key_id]

    async def hash_of(self, key_id: UUID) -> str | None:
        return self.hashes.get(key_id)


class FakeKeyCache:
    def __init__(self) -> None:
        self.entries: dict[str, ApiKey | None] = {}

    async def get(self, key_hash: str) -> ApiKey | bool | None:
        if key_hash not in self.entries:
            return None
        value = self.entries[key_hash]
        return False if value is None else value

    async def put(self, key_hash: str, key: ApiKey | None) -> None:
        self.entries[key_hash] = key

    async def invalidate(self, key_hash: str) -> None:
        self.entries.pop(key_hash, None)


Behaviour = Completion | ProviderFailure | Callable[[], Any]


@dataclass
class FakeProvider:
    """A scripted provider: each call consumes the next behaviour (or repeats the last)."""

    name: str
    behaviours: list[Behaviour] = field(default_factory=list)
    configured: bool = True
    capable: bool = True
    delay_s: float = 0.0
    words: Sequence[str] = ("hello", "from", "the", "fake")
    calls: list[str] = field(default_factory=list)
    stream_failure_after: int | None = None

    def supports(self, request: ChatCompletionRequest) -> bool:
        return self.capable

    def _next(self) -> Behaviour | None:
        if not self.behaviours:
            return None
        return self.behaviours.pop(0) if len(self.behaviours) > 1 else self.behaviours[0]

    async def complete(self, request: ChatCompletionRequest, model: str) -> Completion:
        self.calls.append(f"complete:{model}")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        behaviour = self._next()
        if isinstance(behaviour, ProviderFailure):
            raise behaviour
        if isinstance(behaviour, Completion):
            return behaviour
        if callable(behaviour):
            behaviour()
        return make_completion(" ".join(self.words), model=model)

    async def stream(
        self, request: ChatCompletionRequest, model: str
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(f"stream:{model}")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        behaviour = self._next()
        if isinstance(behaviour, ProviderFailure):
            raise behaviour
        completion_id, created = new_completion_id(), now_ts()
        yield chunk(completion_id, model, created, delta={"role": "assistant", "content": ""})
        for index, word in enumerate(self.words):
            if self.stream_failure_after is not None and index == self.stream_failure_after:
                raise ProviderFailure(self.name, FailureKind.CONNECTION, "reset mid-stream")
            yield chunk(
                completion_id, model, created, delta={"content": (" " if index else "") + word}
            )
        yield chunk(completion_id, model, created, finish_reason="stop")
        yield chunk(completion_id, model, created, usage=Usage(10, len(self.words)))

    async def aclose(self) -> None:
        return None


def make_completion(
    text: str, *, model: str = "fake-model", usage: Usage | None = None
) -> Completion:
    usage = usage or Usage(prompt_tokens=10, completion_tokens=len(text.split()))
    body = {
        "id": new_completion_id(),
        "object": "chat.completion",
        "created": now_ts(),
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": usage.as_openai(),
    }
    return Completion(body=body, usage=usage)


class FakeRateLimiter:
    """Fixed-window stand-in for the Redis limiter (no refill), for service-level tests."""

    def __init__(self) -> None:
        self.requests: dict[str, int] = {}
        self.tokens: dict[str, int] = {}
        self.spend: dict[str, int] = {}
        self.down = False
        self.settled: list[tuple[str, int, int]] = []

    def _check_up(self) -> None:
        if self.down:
            raise DependencyUnavailable("fake limiter is down")

    async def admit(self, check: LimitCheck) -> Admission:
        self._check_up()
        used_requests = self.requests.get(check.key_id, 0)
        used_tokens = self.tokens.get(check.key_id, 0)
        spent = self.spend.get(check.key_id, 0)
        reason = None
        if used_requests + 1 > check.rpm_limit:
            reason = LimitReason.REQUESTS
        elif used_tokens + check.tokens > check.tpd_limit:
            reason = LimitReason.TOKENS
        elif check.budget_micro_usd is not None and spent >= check.budget_micro_usd:
            reason = LimitReason.BUDGET
        if reason is not None:
            return Admission(
                False, reason, 1.5, check.rpm_limit - used_requests, check.tpd_limit - used_tokens
            )
        self.requests[check.key_id] = used_requests + 1
        self.tokens[check.key_id] = used_tokens + check.tokens
        return Admission(
            True,
            None,
            0,
            check.rpm_limit - used_requests - 1,
            check.tpd_limit - used_tokens - check.tokens,
        )

    async def settle(
        self, check: LimitCheck, actual_tokens: int, spend_micro_usd: int
    ) -> tuple[int, int]:
        self._check_up()
        self.settled.append((check.key_id, actual_tokens, spend_micro_usd))
        self.tokens[check.key_id] = self.tokens.get(check.key_id, 0) + actual_tokens - check.tokens
        self.spend[check.key_id] = self.spend.get(check.key_id, 0) + spend_micro_usd
        return check.tpd_limit - self.tokens[check.key_id], self.spend[check.key_id]


class FakeExactCache:
    def __init__(self) -> None:
        self.entries: dict[str, CachedCompletion] = {}

    async def get(self, key: str) -> CachedCompletion | None:
        return self.entries.get(key)

    async def put(self, key: str, entry: CachedCompletion, ttl_s: int) -> None:
        self.entries[key] = entry

    async def flush(self) -> int:
        count = len(self.entries)
        self.entries.clear()
        return count


class FakeSemanticCache:
    """Brute-force cosine search over a list, standing in for pgvector."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, list[float], CachedCompletion]] = []

    async def nearest(self, namespace: str, embedding: Sequence[float]) -> SemanticMatch | None:
        scored = [
            (cosine(list(embedding), vector), entry)
            for ns, vector, entry in self.entries
            if ns == namespace
        ]
        if not scored:
            return None
        similarity, entry = max(scored, key=lambda pair: pair[0])
        return SemanticMatch(entry=entry, similarity=similarity)

    async def put(
        self, namespace: str, embedding: Sequence[float], entry: CachedCompletion, ttl_s: int
    ) -> None:
        self.entries.append((namespace, list(embedding), entry))

    async def purge_expired(self) -> int:
        return 0

    async def flush(self) -> int:
        count = len(self.entries)
        self.entries.clear()
        return count


class FakeUsageStore:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []
        self.daily_rows: dict[tuple[Any, ...], DailyUsageRow] = {}
        self.fail_next = 0
        self.writes = 0

    async def write(self, events: Sequence[UsageEvent], rollups: Sequence[DailyRollup]) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("database down")
        self.writes += 1
        self.events.extend(events)
        for r in rollups:
            key = (r.day, r.api_key_id, r.route, r.provider)
            old = self.daily_rows.get(key)
            values = {
                name: getattr(r, name) + (getattr(old, name) if old else 0)
                for name in (
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
            }
            self.daily_rows[key] = DailyUsageRow(r.day, r.api_key_id, r.route, r.provider, **values)

    async def daily(
        self, since: Any, until: Any, api_key_id: UUID | None = None
    ) -> list[DailyUsageRow]:
        return [
            row
            for row in self.daily_rows.values()
            if since <= row.day <= until and (api_key_id is None or row.api_key_id == api_key_id)
        ]

    async def recent(self, limit: int, api_key_id: UUID | None = None) -> list[UsageEvent]:
        events = [e for e in self.events if api_key_id is None or e.api_key_id == api_key_id]
        return list(reversed(events))[:limit]

    async def purge_before(self, cutoff: Any) -> int:
        before = len(self.events)
        self.events = [e for e in self.events if e.created_at >= cutoff]
        return before - len(self.events)


class FakeAlertStore:
    def __init__(self) -> None:
        self.alerts: list[BudgetAlert] = []

    async def insert_if_new(self, alert: BudgetAlert) -> bool:
        key = (alert.api_key_id, alert.period, alert.threshold_pct)
        if any((a.api_key_id, a.period, a.threshold_pct) == key for a in self.alerts):
            return False
        self.alerts.append(alert)
        return True

    async def recent(self, limit: int) -> list[BudgetAlert]:
        return list(reversed(self.alerts))[:limit]


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[BudgetAlert] = []

    async def notify(self, alert: BudgetAlert) -> None:
        self.sent.append(alert)


class FakeHealthBoard:
    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, Any]] = {}

    async def publish(self, replica: str, snapshot: dict[str, Any], ttl_s: int) -> None:
        self.snapshots[replica] = snapshot

    async def read_all(self) -> dict[str, dict[str, Any]]:
        return dict(self.snapshots)
