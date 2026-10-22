"""In-memory fakes for the service ports, used by unit tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from relay.domain.chat import (
    ChatCompletionRequest,
    Completion,
    Usage,
    chunk,
    new_completion_id,
    now_ts,
)
from relay.domain.errors import Conflict, FailureKind, ProviderFailure
from relay.domain.keys import ApiKey
from relay.services.ports import KeyChanges, StreamEvent


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
