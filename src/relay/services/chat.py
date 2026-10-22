"""The gateway's request pipeline for chat completions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from relay.domain.catalog import Catalog, RouteSpec, RoutingPolicy, TargetSpec
from relay.domain.chat import ChatCompletionRequest, Completion
from relay.domain.errors import (
    AllProvidersFailed,
    FailureKind,
    InvalidRequest,
    ModelNotFound,
    NoProviderAvailable,
    PermissionDenied,
    ProviderFailure,
    RelayError,
    UpstreamRejected,
)
from relay.domain.keys import ApiKey
from relay.services.ports import ChatProvider, StreamEvent


class CacheStatus(StrEnum):
    MISS = "miss"
    EXACT = "exact"
    SEMANTIC = "semantic"
    BYPASS = "bypass"


@dataclass(frozen=True, slots=True)
class CallContext:
    request_id: str
    key: ApiKey
    policy_override: RoutingPolicy | None = None
    cache_lookup: bool = True
    cache_store: bool = True


@dataclass(slots=True)
class ResponseMeta:
    route: str
    provider: str | None = None
    provider_model: str | None = None
    attempts: int = 0
    cache: CacheStatus = CacheStatus.MISS
    extra_headers: dict[str, str] = field(default_factory=dict)

    def http_headers(self) -> dict[str, str]:
        headers = {
            "x-relay-route": self.route,
            "x-relay-cache": self.cache.value,
            "x-relay-attempts": str(self.attempts),
        }
        if self.provider:
            headers["x-relay-provider"] = self.provider
            headers["x-relay-provider-model"] = self.provider_model or ""
        headers.update(self.extra_headers)
        return headers


@dataclass(slots=True)
class ChatResult:
    completion: Completion
    meta: ResponseMeta


@dataclass(slots=True)
class StreamResult:
    events: AsyncIterator[StreamEvent]
    meta: ResponseMeta


class ChatService:
    def __init__(self, catalog: Catalog, providers: Mapping[str, ChatProvider]) -> None:
        self._catalog = catalog
        self._providers = providers

    def list_models(self) -> list[RouteSpec]:
        return list(self._catalog.routes)

    def resolve(self, ctx: CallContext, request: ChatCompletionRequest) -> RouteSpec:
        route = self._catalog.route(request.model)
        if route is None:
            raise ModelNotFound(
                f"The model {request.model!r} does not exist on this gateway. "
                "GET /v1/models lists the available models.",
                param="model",
            )
        if not ctx.key.may_use(route.model):
            raise PermissionDenied(
                f"This API key may not use the model {route.model!r}.", code="model_not_allowed"
            )
        return route

    def candidates(self, route: RouteSpec, request: ChatCompletionRequest) -> list[TargetSpec]:
        """The route's targets that are configured here and can serve this request."""
        configured = [t for t in route.targets if self._providers[t.provider].configured]
        if not configured:
            raise NoProviderAvailable(
                f"No provider behind {route.model!r} is configured on this gateway "
                "(missing provider API keys).",
                code="provider_not_configured",
            )
        capable = [t for t in configured if self._providers[t.provider].supports(request)]
        if not capable:
            raise InvalidRequest(
                f"No provider behind {route.model!r} supports this request "
                "(for example tool calls, images or n > 1).",
                code="unsupported_parameters",
            )
        return capable

    async def complete(self, ctx: CallContext, request: ChatCompletionRequest) -> ChatResult:
        route = self.resolve(ctx, request)
        target = self.candidates(route, request)[0]
        try:
            completion = await self._providers[target.provider].complete(request, target.model)
        except ProviderFailure as failure:
            raise client_error(failure) from failure
        meta = ResponseMeta(
            route=route.model, provider=target.provider, provider_model=target.model, attempts=1
        )
        return ChatResult(completion=completion, meta=meta)

    async def stream(self, ctx: CallContext, request: ChatCompletionRequest) -> StreamResult:
        route = self.resolve(ctx, request)
        target = self.candidates(route, request)[0]
        try:
            first, rest = await open_stream(self._providers[target.provider], request, target.model)
        except ProviderFailure as failure:
            raise client_error(failure) from failure
        meta = ResponseMeta(
            route=route.model, provider=target.provider, provider_model=target.model, attempts=1
        )
        return StreamResult(events=relay_events(request, first, rest), meta=meta)


async def open_stream(
    provider: ChatProvider, request: ChatCompletionRequest, model: str
) -> tuple[StreamEvent, AsyncIterator[StreamEvent]]:
    """Start a provider stream and wait for its first event.

    Nothing is sent to the client until the first event arrives, so a provider that fails
    to start streaming can still be retried or replaced by another one.
    """
    events = aiter(provider.stream(request, model))
    try:
        first = await anext(events)
    except StopAsyncIteration:
        raise ProviderFailure(provider.name, FailureKind.PROTOCOL, "empty stream") from None
    except BaseException:
        await _close(events)
        raise
    return first, events


async def relay_events(
    request: ChatCompletionRequest, first: StreamEvent, rest: AsyncIterator[StreamEvent]
) -> AsyncIterator[StreamEvent]:
    """Forward provider events to the client, after the response has been committed."""
    wants_usage = request.wants_stream_usage
    try:
        shaped = _shape(first, wants_usage)
        if shaped is not None:
            yield shaped
        async for event in rest:
            shaped = _shape(event, wants_usage)
            if shaped is not None:
                yield shaped
    except ProviderFailure as failure:
        raise AllProvidersFailed(
            f"The upstream stream was interrupted: {failure.message}"
        ) from failure
    finally:
        await _close(rest)


def _shape(event: StreamEvent, wants_usage: bool) -> StreamEvent | None:
    """Drop the usage the gateway asked for on the client's behalf, unless it asked too."""
    if wants_usage or "usage" not in event:
        return event
    if not event.get("choices"):
        return None
    return {key: value for key, value in event.items() if key != "usage"}


async def _close(events: AsyncIterator[StreamEvent]) -> None:
    close = getattr(events, "aclose", None)
    if close is not None:
        await close()


def client_error(failure: ProviderFailure) -> RelayError:
    if failure.is_client_error:
        return UpstreamRejected(f"{failure.provider} rejected the request: {failure.message}")
    return AllProvidersFailed(
        f"Provider {failure.provider} failed: {failure.kind}: {failure.message}"
    )
