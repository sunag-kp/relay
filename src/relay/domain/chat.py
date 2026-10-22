"""OpenAI Chat Completions wire format: the one API every client speaks to Relay.

Unknown request fields are kept (`extra="allow"`) and forwarded to OpenAI-compatible
upstreams, so new OpenAI parameters work through the gateway without a release.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Fields that change how the gateway transports a request, never what the model generates.
TRANSPORT_FIELDS = frozenset({"stream", "stream_options", "user"})


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    def text(self) -> str:
        """The message's text, joining multi-part content and ignoring non-text parts."""
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(
            str(part.get("text", "")) for part in self.content if part.get("type") == "text"
        )

    def is_text_only(self) -> bool:
        if isinstance(self.content, list):
            return all(part.get("type") == "text" for part in self.content)
        return self.tool_calls is None and self.tool_call_id is None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=200)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    n: int | None = Field(default=None, ge=1, le=16)
    stop: str | list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None
    user: str | None = None

    @property
    def output_limit(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens

    @property
    def wants_stream_usage(self) -> bool:
        return bool(self.stream_options and self.stream_options.get("include_usage"))

    @property
    def stop_sequences(self) -> list[str]:
        if self.stop is None:
            return []
        return [self.stop] if isinstance(self.stop, str) else list(self.stop)

    def upstream_body(self, model: str) -> dict[str, Any]:
        """The request as an OpenAI-compatible upstream should receive it."""
        body = self.model_dump(exclude_none=True)
        body["model"] = model
        return body

    def generation_params(self) -> dict[str, Any]:
        """Everything that influences the generated output, for cache keys."""
        return {
            key: value
            for key, value in self.model_dump(exclude_none=True).items()
            if key not in TRANSPORT_FIELDS
        }


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_openai(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_openai(cls, raw: dict[str, Any] | None) -> Usage | None:
        if not raw:
            return None
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
        )


@dataclass(frozen=True, slots=True)
class Completion:
    """A finished (non-streamed) chat completion in OpenAI format, plus parsed usage."""

    body: dict[str, Any]
    usage: Usage

    def text(self) -> str:
        choices = self.body.get("choices") or []
        if not choices:
            return ""
        return str((choices[0].get("message") or {}).get("content") or "")


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def now_ts() -> int:
    return int(time.time())


def chunk(
    completion_id: str,
    model: str,
    created: int,
    *,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: Usage | None = None,
) -> dict[str, Any]:
    """Build one `chat.completion.chunk` event."""
    body: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": []
        if delta is None and finish_reason is None
        else [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage.as_openai()
    return body


def completion_to_chunks(completion: Completion, *, include_usage: bool) -> list[dict[str, Any]]:
    """Replay a finished completion as a stream (used for cache hits on `stream=true`)."""
    body = completion.body
    completion_id = str(body.get("id") or new_completion_id())
    model = str(body.get("model") or "")
    created = int(body.get("created") or now_ts())
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    events = [chunk(completion_id, model, created, delta={"role": "assistant", "content": ""})]
    if message.get("content"):
        events.append(chunk(completion_id, model, created, delta={"content": message["content"]}))
    events.append(
        chunk(completion_id, model, created, finish_reason=choice.get("finish_reason") or "stop")
    )
    if include_usage:
        events.append(chunk(completion_id, model, created, usage=completion.usage))
    return events


class StreamAccumulator:
    """Rebuilds a complete response from streamed chunks (for metering and caching)."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.finish_reason: str | None = None
        self.usage: Usage | None = None
        self.has_tool_calls = False
        self.completion_id: str | None = None
        self.model: str | None = None
        self.created: int | None = None

    def add(self, event: dict[str, Any]) -> None:
        self.completion_id = self.completion_id or event.get("id")
        self.model = self.model or event.get("model")
        self.created = self.created or event.get("created")
        usage = Usage.from_openai(event.get("usage"))
        if usage is not None:
            self.usage = usage
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self.parts.append(str(delta["content"]))
            if delta.get("tool_calls"):
                self.has_tool_calls = True
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def to_completion(self, fallback_usage: Usage) -> Completion:
        usage = self.usage or fallback_usage
        body = {
            "id": self.completion_id or new_completion_id(),
            "object": "chat.completion",
            "created": self.created or now_ts(),
            "model": self.model or "",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self.text},
                    "finish_reason": self.finish_reason or "stop",
                }
            ],
            "usage": usage.as_openai(),
        }
        return Completion(body=body, usage=usage)
