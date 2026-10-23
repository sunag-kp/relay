from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from relay.api.deps import get_container
from relay.api.errors import error_body
from relay.domain.catalog import RoutingPolicy
from relay.domain.chat import ChatCompletionRequest
from relay.domain.errors import InvalidRequest, RelayError
from relay.domain.keys import ApiKey
from relay.services.chat import CallContext
from relay.services.ports import StreamEvent

router = APIRouter(tags=["chat"])


async def authenticate(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> ApiKey:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return await get_container(request).keys.authenticate(token)


def _cache_directives(cache_control: str | None) -> tuple[bool, bool]:
    """`Cache-Control: no-cache` skips the lookup, `no-store` skips writing the answer."""
    directives = {part.strip().lower() for part in (cache_control or "").split(",")}
    return "no-cache" not in directives, "no-store" not in directives


def _policy(value: str | None) -> RoutingPolicy | None:
    if value is None:
        return None
    try:
        return RoutingPolicy(value)
    except ValueError:
        allowed = ", ".join(policy.value for policy in RoutingPolicy)
        raise InvalidRequest(f"x-relay-policy must be one of: {allowed}.") from None


@router.post(
    "/v1/chat/completions",
    summary="Create a chat completion (OpenAI-compatible)",
    responses={
        200: {"description": "A `chat.completion`, or an SSE stream of `chat.completion.chunk`."},
        400: {"description": "Invalid request"},
        401: {"description": "Missing or invalid API key"},
        429: {"description": "Rate limit or quota exceeded (see `Retry-After`)"},
        502: {"description": "Every provider attempt failed"},
        503: {"description": "Every provider is short-circuited by its circuit breaker"},
    },
)
async def create_chat_completion(
    request: Request,
    body: ChatCompletionRequest,
    key: Annotated[ApiKey, Depends(authenticate)],
    cache_control: Annotated[str | None, Header()] = None,
    x_relay_policy: Annotated[
        str | None, Header(description="Override the route's routing policy")
    ] = None,
) -> Any:
    container = get_container(request)
    lookup, store = _cache_directives(cache_control)
    ctx = CallContext(
        request_id=request.scope["state"]["request_id"],
        key=key,
        policy_override=_policy(x_relay_policy),
        cache_lookup=lookup,
        cache_store=store,
    )
    if body.stream:
        result = await container.chat.stream(ctx, body)
        return StreamingResponse(
            _sse(result.events),
            media_type="text/event-stream",
            headers={
                **result.meta.http_headers(),
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
        )
    chat_result = await container.chat.complete(ctx, body)
    return JSONResponse(chat_result.completion.body, headers=chat_result.meta.http_headers())


async def _sse(events: AsyncIterator[StreamEvent]) -> AsyncIterator[bytes]:
    try:
        async for event in events:
            yield b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"
    except RelayError as exc:
        # Headers are already sent, so a mid-stream failure becomes an error event, which
        # the OpenAI SDKs raise as an APIError.
        payload = error_body(exc.message, exc.error_type, exc.code)
        yield b"data: " + json.dumps(payload).encode() + b"\n\n"
    yield b"data: [DONE]\n\n"


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "relay"
    policy: str
    targets: list[str]
    description: str = ""


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelCard]


@router.get("/v1/models", response_model=ModelList, summary="List routable models")
async def list_models(request: Request, key: Annotated[ApiKey, Depends(authenticate)]) -> ModelList:
    """The routes this key may call."""
    routes = [r for r in get_container(request).chat.list_models() if key.may_use(r.model)]
    return ModelList(
        data=[
            ModelCard(
                id=route.model,
                policy=route.policy.value,
                targets=[target.key for target in route.targets],
                description=route.description,
            )
            for route in routes
        ]
    )
