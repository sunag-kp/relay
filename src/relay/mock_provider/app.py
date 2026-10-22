"""A mock LLM provider for tests, load tests and the free live demo.

One server impersonates any number of providers ("profiles"), each speaking both the OpenAI
Chat Completions and the Anthropic Messages wire formats:

    /v1/chat/completions             profile "default", OpenAI format
    /p/{profile}/v1/chat/completions OpenAI format
    /p/{profile}/v1/messages         Anthropic format

Each profile has its own latency, jitter and failure rate, set at startup through
`MOCK_PROFILES` (JSON) and changed at runtime through `PUT /_control/profiles/{name}`, so a
chaos script can make one "provider" fail and watch the gateway's breaker trip and fall back.
Per-request overrides: `x-mock-status: 503` forces an error, `x-mock-latency-ms: 2000` a delay.

Built on plain Starlette (no validation layer) so it is never the bottleneck in a load test.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, fields
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

FILLER = (
    "Relay routes this request across providers with retries circuit breakers and caching "
    "so the application only ever talks to one endpoint while the gateway handles limits "
    "costs and failures on its behalf"
).split()


@dataclass
class Profile:
    latency_ms: float = 40.0  # time to first byte
    jitter_ms: float = 10.0
    failure_rate: float = 0.0
    failure_status: int = 503
    midstream_failure_rate: float = 0.0
    stream_chunk_delay_ms: float = 2.0
    completion_words: int = 24

    def update(self, values: dict[str, Any]) -> None:
        names = {f.name for f in fields(self)}
        for key, value in values.items():
            if key not in names:
                raise ValueError(f"unknown profile field {key!r}")
            setattr(self, key, type(getattr(self, key))(value))


class MockState:
    def __init__(self, profiles: dict[str, Profile]) -> None:
        self.initial = {name: asdict(profile) for name, profile in profiles.items()}
        self.profiles = profiles
        self.requests = 0

    def profile(self, name: str) -> Profile:
        if name not in self.profiles:
            self.profiles[name] = Profile()
        return self.profiles[name]

    def reset(self) -> None:
        self.profiles = {name: Profile(**values) for name, values in self.initial.items()}
        self.requests = 0


def _load_profiles() -> dict[str, Profile]:
    raw = os.environ.get("MOCK_PROFILES")
    profiles = {"default": Profile()}
    if raw:
        for name, values in json.loads(raw).items():
            profile = Profile()
            profile.update(values)
            profiles[name] = profile
    return profiles


# ---------------------------------------------------------------- content


def _texts(messages: list[dict[str, Any]]) -> list[str]:
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        out.append(str(content or ""))
    return out


def _prompt_tokens(texts: list[str], system: str = "") -> int:
    return sum(len(text) // 4 + 4 for text in texts) + (len(system) // 4 if system else 0)


def _reply_words(profile_name: str, last_user: str, words: int, limit: int | None) -> list[str]:
    echo = last_user.split()[:12]
    body = [f"[{profile_name}]", "You", "said:", *echo, "--"]
    while len(body) < words:
        body.extend(FILLER)
    body = body[:words]
    return body[:limit] if limit else body


async def _delay(profile: Profile, override: str | None) -> None:
    latency = float(override) if override else profile.latency_ms
    jitter = random.uniform(-profile.jitter_ms, profile.jitter_ms) if profile.jitter_ms else 0.0  # noqa: S311
    await asyncio.sleep(max(0.0, latency + jitter) / 1000)


def _forced_status(request: Request, profile: Profile) -> int | None:
    forced = request.headers.get("x-mock-status")
    if forced:
        return int(forced)
    if profile.failure_rate and random.random() < profile.failure_rate:  # noqa: S311
        return profile.failure_status
    return None


def _error(status: int, *, anthropic: bool) -> JSONResponse:
    message = f"mock provider failure ({status})"
    if anthropic:
        error_type = "overloaded_error" if status == 529 else "api_error"
        body: dict[str, Any] = {"type": "error", "error": {"type": error_type, "message": message}}
    else:
        body = {"error": {"message": message, "type": "server_error", "code": str(status)}}
    headers = {"retry-after": "1"} if status == 429 else None
    return JSONResponse(body, status_code=status, headers=headers)


# ---------------------------------------------------------------- OpenAI format


async def _openai(request: Request, state: MockState, profile_name: str) -> Response:
    state.requests += 1
    profile = state.profile(profile_name)
    payload = json.loads(await request.body())
    messages = payload.get("messages") or []
    texts = _texts(messages)
    last_user = next(
        (
            t
            for m, t in zip(reversed(messages), reversed(texts), strict=True)
            if m.get("role") == "user"
        ),
        "",
    )
    limit = payload.get("max_completion_tokens") or payload.get("max_tokens")
    words = _reply_words(profile_name, last_user, profile.completion_words, limit)
    usage = {
        "prompt_tokens": _prompt_tokens(texts),
        "completion_tokens": len(words),
        "total_tokens": _prompt_tokens(texts) + len(words),
    }
    model = str(payload.get("model", "mock"))
    completion_id = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    finish = "length" if limit and limit <= len(words) else "stop"

    await _delay(profile, request.headers.get("x-mock-latency-ms"))
    status = _forced_status(request, profile)
    if status:
        return _error(status, anthropic=False)

    if not payload.get("stream"):
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": " ".join(words)},
                        "finish_reason": finish,
                    }
                ],
                "usage": usage,
            }
        )

    include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
    fail_mid = random.random() < profile.midstream_failure_rate  # noqa: S311

    def event(choices: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> bytes:
        body = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": choices,
            **(extra or {}),
        }
        return b"data: " + json.dumps(body).encode() + b"\n\n"

    async def body() -> AsyncIterator[bytes]:
        yield event(
            [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
        )
        for i, word in enumerate(words):
            if fail_mid and i == len(words) // 2:
                raise ConnectionAbortedError("mock mid-stream failure")
            if profile.stream_chunk_delay_ms:
                await asyncio.sleep(profile.stream_chunk_delay_ms / 1000)
            text = word if i == 0 else " " + word
            yield event([{"index": 0, "delta": {"content": text}, "finish_reason": None}])
        yield event([{"index": 0, "delta": {}, "finish_reason": finish}])
        if include_usage:
            yield event([], {"usage": usage})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


# ---------------------------------------------------------------- Anthropic format


async def _anthropic(request: Request, state: MockState, profile_name: str) -> Response:
    state.requests += 1
    profile = state.profile(profile_name)
    payload = json.loads(await request.body())
    if "max_tokens" not in payload:
        return JSONResponse(
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "max_tokens: Field required"},
            },
            status_code=400,
        )
    for forbidden in ("temperature", "top_p", "top_k"):
        if forbidden in payload:
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"{forbidden}: not supported",
                    },
                },
                status_code=400,
            )
    messages = payload.get("messages") or []
    texts = _texts(messages)
    last_user = next(
        (
            t
            for m, t in zip(reversed(messages), reversed(texts), strict=True)
            if m["role"] == "user"
        ),
        "",
    )
    words = _reply_words(
        profile_name, last_user, profile.completion_words, int(payload["max_tokens"])
    )
    input_tokens = _prompt_tokens(texts, str(payload.get("system") or ""))
    model = str(payload.get("model", "mock"))
    message_id = f"msg_mock_{uuid.uuid4().hex[:12]}"
    stop_reason = "max_tokens" if int(payload["max_tokens"]) <= len(words) else "end_turn"

    await _delay(profile, request.headers.get("x-mock-latency-ms"))
    status = _forced_status(request, profile)
    if status:
        return _error(status, anthropic=True)

    if not payload.get("stream"):
        return JSONResponse(
            {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": " ".join(words)}],
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": len(words)},
            }
        )

    def event(name: str, body: dict[str, Any]) -> bytes:
        return f"event: {name}\ndata: {json.dumps(body)}\n\n".encode()

    async def body() -> AsyncIterator[bytes]:
        yield event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 1},
                },
            },
        )
        yield event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        yield event("ping", {"type": "ping"})
        for i, word in enumerate(words):
            if profile.stream_chunk_delay_ms:
                await asyncio.sleep(profile.stream_chunk_delay_ms / 1000)
            text = word if i == 0 else " " + word
            yield event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        yield event("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": len(words)},
            },
        )
        yield event("message_stop", {"type": "message_stop"})

    return StreamingResponse(body(), media_type="text/event-stream")


# ---------------------------------------------------------------- app


def create_mock_app(profiles: dict[str, Profile] | None = None) -> Starlette:
    state = MockState(profiles if profiles is not None else _load_profiles())

    async def openai_default(request: Request) -> Response:
        return await _openai(request, state, "default")

    async def openai_profile(request: Request) -> Response:
        return await _openai(request, state, request.path_params["profile"])

    async def anthropic_default(request: Request) -> Response:
        return await _anthropic(request, state, "default")

    async def anthropic_profile(request: Request) -> Response:
        return await _anthropic(request, state, request.path_params["profile"])

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "requests": state.requests})

    async def list_profiles(_request: Request) -> Response:
        return JSONResponse({name: asdict(p) for name, p in state.profiles.items()})

    async def put_profile(request: Request) -> Response:
        profile = state.profile(request.path_params["name"])
        try:
            profile.update(json.loads(await request.body() or b"{}"))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(asdict(profile))

    async def reset(_request: Request) -> Response:
        state.reset()
        return JSONResponse({"status": "reset"})

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/v1/chat/completions", openai_default, methods=["POST"]),
            Route("/v1/messages", anthropic_default, methods=["POST"]),
            Route("/p/{profile}/v1/chat/completions", openai_profile, methods=["POST"]),
            Route("/p/{profile}/v1/messages", anthropic_profile, methods=["POST"]),
            Route("/_control/profiles", list_profiles),
            Route("/_control/profiles/{name}", put_profile, methods=["PUT"]),
            Route("/_control/reset", reset, methods=["POST"]),
        ]
    )
    app.state.mock = state
    return app
