"""Response caching rules: what is cacheable, cache keys and namespaces.

Two layers:

* **Exact**: a SHA-256 of the canonical request (model alias + every generation
  parameter). Identical requests hit; anything else misses.
* **Semantic**: an embedding of the conversation, looked up by cosine similarity inside a
  namespace that pins everything except the wording (owner, model, parameters, embedder).
  "What's the capital of France?" can then reuse the answer to "what is the capital of
  france". Only short, text-only conversations qualify: long prompts rarely repeat, and a
  near-miss on a long prompt is more likely to be a different question.

Entries are partitioned by key owner, so one tenant's prompts are never served to another.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from relay.domain.chat import ChatCompletionRequest, Completion, Usage, new_completion_id, now_ts

CACHE_VERSION = "v1"
SEMANTIC_MAX_CHARS = 4000


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def exact_key(owner: str, request: ChatCompletionRequest) -> str:
    return _digest(CACHE_VERSION, owner, _canonical(request.generation_params()))


def semantic_namespace(owner: str, request: ChatCompletionRequest, embedder_id: str) -> str:
    params = {k: v for k, v in request.generation_params().items() if k != "messages"}
    return _digest(CACHE_VERSION, owner, embedder_id, _canonical(params))[:32]


def semantic_text(request: ChatCompletionRequest) -> str | None:
    """The text to embed, or None if this request should not use the semantic cache."""
    if request.tools or (request.n or 1) > 1:
        return None
    if not all(m.is_text_only() for m in request.messages):
        return None
    text = "\n".join(f"{m.role}: {m.text()}" for m in request.messages)
    if len(text) > SEMANTIC_MAX_CHARS:
        return None
    return text


def is_cacheable(request: ChatCompletionRequest) -> bool:
    return (request.n or 1) == 1


def is_storable(completion: Completion) -> bool:
    choices = completion.body.get("choices") or []
    if len(choices) != 1:
        return False
    return choices[0].get("finish_reason") in {"stop", "length", "tool_calls"}


@dataclass(frozen=True, slots=True)
class CachedCompletion:
    body: dict[str, Any]
    usage: Usage
    provider: str
    provider_model: str
    cost_usd: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "body": self.body,
            "usage": self.usage.as_openai(),
            "provider": self.provider,
            "provider_model": self.provider_model,
            "cost_usd": str(self.cost_usd),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CachedCompletion:
        return cls(
            body=data["body"],
            usage=Usage.from_openai(data["usage"]) or Usage(),
            provider=data["provider"],
            provider_model=data["provider_model"],
            cost_usd=Decimal(data["cost_usd"]),
        )

    def fresh_completion(self) -> Completion:
        """The cached answer under a new id and timestamp, so clients never see duplicates."""
        body = {**self.body, "id": new_completion_id(), "created": now_ts()}
        return Completion(body=body, usage=self.usage)
