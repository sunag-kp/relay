"""The gateway's request pipeline for chat completions."""

from __future__ import annotations

import random
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from relay.domain import routing
from relay.domain.catalog import Catalog, RouteSpec, RoutingPolicy, TargetSpec
from relay.domain.chat import ChatCompletionRequest, Completion, StreamAccumulator, Usage
from relay.domain.errors import (
    AllProvidersFailed,
    InvalidRequest,
    ModelNotFound,
    NoProviderAvailable,
    PermissionDenied,
    ProviderFailure,
    RelayError,
)
from relay.domain.keys import ApiKey
from relay.domain.tokens import estimate_prompt_tokens, estimate_text_tokens
from relay.observability import metrics
from relay.services.background import BackgroundTasks
from relay.services.limits import QuotaService, Reservation
from relay.services.ports import ChatProvider, StreamEvent
from relay.services.resilience import (
    Attempted,
    ProviderHealth,
    ResilientExecutor,
    close_stream,
)


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

    def served_by(self, attempted: Attempted) -> None:
        self.provider = attempted.target.provider
        self.provider_model = attempted.target.model
        self.attempts = attempted.attempts


@dataclass(slots=True)
class ChatResult:
    completion: Completion
    meta: ResponseMeta


@dataclass(slots=True)
class StreamResult:
    events: AsyncIterator[StreamEvent]
    meta: ResponseMeta


class ChatService:
    def __init__(
        self,
        catalog: Catalog,
        providers: Mapping[str, ChatProvider],
        health: ProviderHealth,
        executor: ResilientExecutor,
        quotas: QuotaService,
        background: BackgroundTasks,
        *,
        request_timeout_s: float = 120.0,
        default_completion_tokens: int = 256,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._catalog = catalog
        self._providers = providers
        self._health = health
        self._executor = executor
        self._quotas = quotas
        self._background = background
        self._request_timeout_s = request_timeout_s
        self._default_completion_tokens = default_completion_tokens
        self._rng = rng or random.Random()  # noqa: S311 - load spreading, not cryptography
        self._clock = clock

    @property
    def health(self) -> ProviderHealth:
        return self._health

    def list_models(self) -> list[RouteSpec]:
        return list(self._catalog.routes)

    # ------------------------------------------------------------ routing

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

    def plan(
        self, ctx: CallContext, route: RouteSpec, request: ChatCompletionRequest
    ) -> list[TargetSpec]:
        inputs = routing.RoutingInputs(
            prompt_tokens=estimate_prompt_tokens(request),
            completion_tokens=request.output_limit or self._default_completion_tokens,
            latency_ms=lambda target: self._health.latency_ms(target, stream=request.stream),
            rng=self._rng,
        )
        policy = ctx.policy_override or route.policy
        return routing.plan(policy, self.candidates(route, request), inputs)

    # ------------------------------------------------------------ calls

    async def complete(self, ctx: CallContext, request: ChatCompletionRequest) -> ChatResult:
        started = self._clock()
        route = self.resolve(ctx, request)
        reservation = await self._quotas.admit(ctx.key, request)
        meta = ResponseMeta(route=route.model, extra_headers=reservation.headers())
        try:
            plan = self.plan(ctx, route, request)
            completion, attempted = await self._executor.complete(
                plan, request, started + self._request_timeout_s
            )
        except RelayError as exc:
            self._failed(route, reservation, meta, exc, stream=False, started=started)
            raise
        meta.served_by(attempted)
        _record_success(route.model, meta, attempted, stream=False, seconds=self._clock() - started)
        self._account(reservation, attempted.target, completion.usage)
        return ChatResult(completion=completion, meta=meta)

    async def stream(self, ctx: CallContext, request: ChatCompletionRequest) -> StreamResult:
        started = self._clock()
        route = self.resolve(ctx, request)
        reservation = await self._quotas.admit(ctx.key, request)
        meta = ResponseMeta(route=route.model, extra_headers=reservation.headers())
        try:
            plan = self.plan(ctx, route, request)
            opened = await self._executor.open_stream(
                plan, request, started + self._request_timeout_s
            )
        except RelayError as exc:
            self._failed(route, reservation, meta, exc, stream=True, started=started)
            raise
        meta.served_by(opened.attempted)
        elapsed = self._clock() - started
        metrics.TIME_TO_FIRST_TOKEN.labels(route=route.model).observe(elapsed)
        _record_success(route.model, meta, opened.attempted, stream=True, seconds=elapsed)
        events = self._relay(request, opened.first, opened.rest, opened.attempted, reservation)
        return StreamResult(events=events, meta=meta)

    async def _relay(
        self,
        request: ChatCompletionRequest,
        first: StreamEvent,
        rest: AsyncIterator[StreamEvent],
        attempted: Attempted,
        reservation: Reservation,
    ) -> AsyncIterator[StreamEvent]:
        """Forward provider events to the client, after the response has been committed."""
        wants_usage = request.wants_stream_usage
        accumulated = StreamAccumulator()
        try:
            accumulated.add(first)
            shaped = _shape(first, wants_usage)
            if shaped is not None:
                yield shaped
            async for event in rest:
                accumulated.add(event)
                shaped = _shape(event, wants_usage)
                if shaped is not None:
                    yield shaped
        except ProviderFailure as failure:
            # Too late to fall back: the client already has part of an answer.
            self._executor.record_stream_failure(attempted.target.provider)
            raise AllProvidersFailed(
                f"The upstream stream was interrupted: {failure.message}"
            ) from failure
        finally:
            # Runs on success, upstream failure and client disconnect alike: whatever was
            # generated is billed (estimated from the text if the provider never said).
            usage = accumulated.usage or Usage(
                prompt_tokens=estimate_prompt_tokens(request),
                completion_tokens=estimate_text_tokens(accumulated.text),
            )
            self._account(reservation, attempted.target, usage)
            await close_stream(rest)

    # ------------------------------------------------------------ accounting

    def _account(self, reservation: Reservation, target: TargetSpec, usage: Usage) -> None:
        cost = target.cost(usage.prompt_tokens, usage.completion_tokens)
        metrics.TOKENS.labels(provider=target.provider, model=target.model, kind="prompt").inc(
            usage.prompt_tokens
        )
        metrics.TOKENS.labels(provider=target.provider, model=target.model, kind="completion").inc(
            usage.completion_tokens
        )
        metrics.COST.labels(provider=target.provider, model=target.model).inc(float(cost))
        self._background.spawn(
            self._quotas.settle(reservation, tokens=usage.total_tokens, cost_usd=cost),
            name="settle",
        )

    def _failed(
        self,
        route: RouteSpec,
        reservation: Reservation,
        meta: ResponseMeta,
        exc: RelayError,
        *,
        stream: bool,
        started: float,
    ) -> None:
        _record_request(route.model, exc.code, meta, stream=stream, seconds=self._clock() - started)
        # The request still counts against requests/minute, but its token reservation is
        # given back: nothing was generated.
        self._background.spawn(
            self._quotas.settle(reservation, tokens=0, cost_usd=Decimal(0)), name="refund"
        )


def _shape(event: StreamEvent, wants_usage: bool) -> StreamEvent | None:
    """Drop the usage the gateway asked for on the client's behalf, unless it asked too."""
    if wants_usage or "usage" not in event:
        return event
    if not event.get("choices"):
        return None
    return {key: value for key, value in event.items() if key != "usage"}


def _record_request(
    route: str, status: str, meta: ResponseMeta, *, stream: bool, seconds: float
) -> None:
    metrics.REQUESTS.labels(route=route, status=status, cache=meta.cache.value).inc()
    metrics.REQUEST_DURATION.labels(
        route=route, stream=str(stream).lower(), cache=meta.cache.value
    ).observe(seconds)


def _record_success(
    route: str, meta: ResponseMeta, attempted: Attempted, *, stream: bool, seconds: float
) -> None:
    _record_request(route, "ok", meta, stream=stream, seconds=seconds)
    metrics.GATEWAY_OVERHEAD.labels(stream=str(stream).lower()).observe(
        max(0.0, seconds - attempted.upstream_s)
    )
    if attempted.fallback:
        metrics.FALLBACKS.labels(route=route, provider=attempted.target.provider).inc()
