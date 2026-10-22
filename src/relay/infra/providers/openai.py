"""Adapter for OpenAI and any OpenAI-compatible server: requests pass through unchanged."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from relay.domain.catalog import ProviderKind, ProviderSpec
from relay.domain.chat import ChatCompletionRequest, Completion, Usage
from relay.domain.errors import FailureKind, ProviderFailure
from relay.domain.tokens import estimate_prompt_tokens, estimate_text_tokens
from relay.infra.providers.http import (
    api_key_for,
    build_client,
    loads_event,
    open_sse,
    post_json,
    stream_timeout,
)
from relay.services.ports import StreamEvent


class OpenAIProvider:
    def __init__(self, spec: ProviderSpec, client: httpx.AsyncClient | None = None) -> None:
        self.spec = spec
        api_key = api_key_for(spec)
        self._configured = spec.api_key_env is None or bool(api_key)
        headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or build_client(spec, headers)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def configured(self) -> bool:
        return self._configured

    def supports(self, request: ChatCompletionRequest) -> bool:
        return True

    def _body(self, request: ChatCompletionRequest, model: str) -> dict[str, Any]:
        body = request.upstream_body(model)
        if self.spec.kind is ProviderKind.OPENAI and "max_tokens" in body:
            # OpenAI deprecated `max_tokens` for chat models; reasoning models reject it.
            body.setdefault("max_completion_tokens", body.pop("max_tokens"))
        return body

    async def complete(self, request: ChatCompletionRequest, model: str) -> Completion:
        body = self._body(request, model)
        body["stream"] = False
        body.pop("stream_options", None)
        payload = await post_json(self._client, self.name, "/chat/completions", body)
        if not payload.get("choices"):
            raise ProviderFailure(self.name, FailureKind.PROTOCOL, "response has no choices")
        usage = Usage.from_openai(payload.get("usage")) or _estimate(request, payload)
        return Completion(body=payload, usage=usage)

    async def stream(
        self, request: ChatCompletionRequest, model: str
    ) -> AsyncIterator[StreamEvent]:
        body = self._body(request, model)
        body["stream"] = True
        # Always ask for usage so metering is exact; the gateway strips it again for
        # clients that did not ask for it.
        body["stream_options"] = {**(request.stream_options or {}), "include_usage": True}
        async with open_sse(
            self._client, self.name, "/chat/completions", body, stream_timeout(self.spec)
        ) as events:
            async for event in events:
                if event.data.strip() == "[DONE]":
                    return
                yield loads_event(self.name, event.data)

    async def aclose(self) -> None:
        await self._client.aclose()


def _estimate(request: ChatCompletionRequest, payload: dict[str, Any]) -> Usage:
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    return Usage(
        prompt_tokens=estimate_prompt_tokens(request),
        completion_tokens=estimate_text_tokens(str(message.get("content") or "")),
    )
