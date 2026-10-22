"""Adapter for the Anthropic Messages API: translates OpenAI-format requests, responses and
stream events in both directions, so clients only ever see the OpenAI wire format."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from relay.domain.catalog import ProviderSpec
from relay.domain.chat import (
    ChatCompletionRequest,
    Completion,
    Usage,
    chunk,
    new_completion_id,
    now_ts,
)
from relay.domain.errors import FailureKind, ProviderFailure
from relay.infra.providers.http import (
    api_key_for,
    build_client,
    loads_event,
    open_sse,
    post_json,
    stream_timeout,
)
from relay.services.ports import StreamEvent

API_VERSION = "2026-10-21"
# The Messages API requires max_tokens; used when the client leaves it out.
DEFAULT_MAX_TOKENS = 4096

FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class AnthropicProvider:
    def __init__(self, spec: ProviderSpec, client: httpx.AsyncClient | None = None) -> None:
        self.spec = spec
        headers = {"anthropic-version": API_VERSION}
        api_key = api_key_for(spec)
        self._configured = spec.api_key_env is None or bool(api_key)
        if api_key:
            headers["x-api-key"] = api_key
        self._client = client or build_client(spec, headers)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def configured(self) -> bool:
        return self._configured

    def supports(self, request: ChatCompletionRequest) -> bool:
        # Text chat only: tool-call and multi-choice translation are not implemented, so the
        # router sends such requests to OpenAI-format targets instead.
        return (
            not request.tools
            and (request.n or 1) == 1
            and all(message.is_text_only() for message in request.messages)
            and all(
                message.role in {"system", "developer", "user", "assistant"}
                for message in request.messages
            )
        )

    async def complete(self, request: ChatCompletionRequest, model: str) -> Completion:
        payload = await post_json(
            self._client, self.name, "/v1/messages", to_anthropic(request, model)
        )
        return from_anthropic(payload, self.name)

    async def stream(
        self, request: ChatCompletionRequest, model: str
    ) -> AsyncIterator[StreamEvent]:
        body = to_anthropic(request, model)
        body["stream"] = True
        translator = StreamTranslator(self.name)
        async with open_sse(
            self._client, self.name, "/v1/messages", body, stream_timeout(self.spec)
        ) as events:
            async for event in events:
                for out in translator.feed(loads_event(self.name, event.data)):
                    yield out
                if translator.done:
                    return
        if not translator.done:
            raise ProviderFailure(
                self.name, FailureKind.PROTOCOL, "stream ended before message_stop"
            )

    async def aclose(self) -> None:
        await self._client.aclose()


def to_anthropic(request: ChatCompletionRequest, model: str) -> dict[str, Any]:
    """OpenAI chat request -> Messages API request.

    `temperature`/`top_p` are not forwarded: API version 2026-10-21 rejects sampling
    parameters on every model, so sending them would turn every request into a 400.
    """
    system = "\n\n".join(m.text() for m in request.messages if m.role in {"system", "developer"})
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role in {"system", "developer"}:
            continue
        role = "assistant" if message.role == "assistant" else "user"
        text = message.text()
        if messages and messages[-1]["role"] == role:
            # The Messages API expects alternating turns; merge consecutive same-role messages.
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": role, "content": text})
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": request.output_limit or DEFAULT_MAX_TOKENS,
        "messages": messages,
    }
    if system:
        body["system"] = system
    if request.stop_sequences:
        body["stop_sequences"] = request.stop_sequences
    if request.user:
        body["metadata"] = {"user_id": request.user}
    return body


def from_anthropic(payload: dict[str, Any], provider: str) -> Completion:
    if payload.get("type") != "message":
        raise ProviderFailure(provider, FailureKind.PROTOCOL, "unexpected response type")
    text = "".join(
        str(block.get("text", ""))
        for block in payload.get("content") or []
        if block.get("type") == "text"
    )
    usage = _usage(payload.get("usage") or {})
    body = {
        "id": f"chatcmpl-{payload.get('id', new_completion_id())}",
        "object": "chat.completion",
        "created": now_ts(),
        "model": payload.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": FINISH_REASONS.get(str(payload.get("stop_reason")), "stop"),
            }
        ],
        "usage": usage.as_openai(),
    }
    return Completion(body=body, usage=usage)


def _usage(raw: dict[str, Any], *, output_tokens: int | None = None) -> Usage:
    # Cache reads and writes are prompt tokens too; Anthropic reports them separately.
    prompt = (
        int(raw.get("input_tokens") or 0)
        + int(raw.get("cache_creation_input_tokens") or 0)
        + int(raw.get("cache_read_input_tokens") or 0)
    )
    completion = output_tokens if output_tokens is not None else int(raw.get("output_tokens") or 0)
    return Usage(prompt_tokens=prompt, completion_tokens=completion)


class StreamTranslator:
    """Messages API stream events -> `chat.completion.chunk` events."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.completion_id = new_completion_id()
        self.created = now_ts()
        self.model = ""
        self.prompt_usage: dict[str, Any] = {}
        self.output_tokens = 0
        self.done = False

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:  # noqa: PLR0911 - one branch per event type
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.completion_id = f"chatcmpl-{message.get('id', self.completion_id)}"
            self.model = str(message.get("model", ""))
            self.prompt_usage = message.get("usage") or {}
            return [self._chunk(delta={"role": "assistant", "content": ""})]
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [self._chunk(delta={"content": delta["text"]})]
            return []  # thinking / signature deltas are not part of the OpenAI format
        if kind == "message_delta":
            usage = event.get("usage") or {}
            self.output_tokens = int(usage.get("output_tokens") or self.output_tokens)
            reason = (event.get("delta") or {}).get("stop_reason")
            if reason:
                return [self._chunk(finish_reason=FINISH_REASONS.get(str(reason), "stop"))]
            return []
        if kind == "message_stop":
            self.done = True
            usage = _usage(self.prompt_usage, output_tokens=self.output_tokens)
            return [chunk(self.completion_id, self.model, self.created, usage=usage)]
        if kind == "error":
            error = event.get("error") or {}
            raise ProviderFailure(
                self.provider,
                FailureKind.SERVER,
                f"{error.get('type', 'error')}: {error.get('message', 'stream error')}",
            )
        return []  # ping, content_block_start/stop

    def _chunk(
        self, *, delta: dict[str, Any] | None = None, finish_reason: str | None = None
    ) -> StreamEvent:
        return chunk(
            self.completion_id, self.model, self.created, delta=delta, finish_reason=finish_reason
        )
