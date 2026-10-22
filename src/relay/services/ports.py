"""Ports: the interfaces the services layer needs from the outside world.

Infrastructure adapters (`relay.infra`) implement these; tests substitute in-memory fakes.
Services never import an adapter, which `import-linter` enforces in CI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from relay.domain.chat import ChatCompletionRequest, Completion
from relay.domain.keys import ApiKey
from relay.domain.limits import Admission, LimitCheck

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
