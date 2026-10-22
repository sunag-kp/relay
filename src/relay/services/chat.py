"""The gateway's request pipeline for chat completions.

authenticate -> resolve route -> admit (rate limits) -> cache lookup
  hit:  serve the cached answer, refund the token reservation
  miss: plan targets -> resilient call (retries, breaker, fallback) -> respond
        -> settle quotas, store in cache (both in the background)
"""

from __future__ import annotations

import random
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from relay.domain import routing
from relay.domain.cache import CachedCompletion, is_storable
from relay.domain.catalog import Catalog, RouteSpec, RoutingPolicy, TargetSpec
from relay.domain.chat import (
    ChatCompletionRequest,
    Completion,
    StreamAccumulator,
    Usage,
    completion_to_chunks,
)
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
from relay.domain.usage import UsageEvent
from relay.observability import metrics
from relay.services.alerts import BudgetAlerts
from relay.services.background import BackgroundTasks
from relay.services.cache import CacheHit, CacheProbe, ResponseCache
from relay.services.limits import QuotaService, Reservation
from relay.services.ports import ChatProvider, StreamEvent
from relay.services.resilience import (
    Attempted,
    ProviderHealth,
    ResilientExecutor,
    close_stream,
)
from relay.services.usage import UsageRecorder


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
        cache: ResponseCache | None = None,
        usage: UsageRecorder | None = None,
        alerts: BudgetAlerts | None = None,
        replica: str = "",
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
        self._cache = cache
        self._usage = usage
        self._alerts = alerts
        self._replica = replica
        self._request_timeout_s = request_timeout_s
        self._default_completion_tokens = default_completion_tokens
        self._rng = rng or random.Random()  # noqa: S311 - load spreading, not cryptography
        self._clock = clock

    @property
    def health(self) -> ProviderHealth:
        return self._health

    @property
    def cache(self) -> ResponseCache | None:
        return self._cache

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
        call = await self._begin(ctx, request, stream=False)
        hit, probe = await self._lookup(ctx, request, call.meta)
        if hit is not None:
            self._served_from_cache(call, hit)
            return ChatResult(completion=hit.entry.fresh_completion(), meta=call.meta)

        try:
            plan = self.plan(ctx, call.route, request)
            completion, attempted = await self._executor.complete(
                plan, request, call.started + self._request_timeout_s
            )
        except RelayError as exc:
            self._failed(call, exc)
            raise
        call.meta.served_by(attempted)
        _record_success(call, attempted, seconds=self._clock() - call.started)
        cost = self._finish(call, attempted, completion.usage)
        if ctx.cache_store and is_storable(completion):
            self._remember(ctx, request, probe, completion, attempted.target, cost)
        return ChatResult(completion=completion, meta=call.meta)

    async def stream(self, ctx: CallContext, request: ChatCompletionRequest) -> StreamResult:
        call = await self._begin(ctx, request, stream=True)
        hit, probe = await self._lookup(ctx, request, call.meta)
        if hit is not None:
            self._served_from_cache(call, hit)
            chunks = completion_to_chunks(
                hit.entry.fresh_completion(), include_usage=request.wants_stream_usage
            )
            return StreamResult(events=_replay(chunks), meta=call.meta)

        try:
            plan = self.plan(ctx, call.route, request)
            opened = await self._executor.open_stream(
                plan, request, call.started + self._request_timeout_s
            )
        except RelayError as exc:
            self._failed(call, exc)
            raise
        call.meta.served_by(opened.attempted)
        elapsed = self._clock() - call.started
        metrics.TIME_TO_FIRST_TOKEN.labels(route=call.route.model).observe(elapsed)
        _record_success(call, opened.attempted, seconds=elapsed)
        events = self._relay(call, request, opened.first, opened.rest, opened.attempted, probe)
        return StreamResult(events=events, meta=call.meta)

    async def _begin(
        self, ctx: CallContext, request: ChatCompletionRequest, *, stream: bool
    ) -> _Call:
        started = self._clock()
        route = self.resolve(ctx, request)
        reservation = await self._quotas.admit(ctx.key, request)
        meta = ResponseMeta(route=route.model, extra_headers=reservation.headers())
        return _Call(ctx, route, reservation, meta, started, stream)

    async def _relay(
        self,
        call: _Call,
        request: ChatCompletionRequest,
        first: StreamEvent,
        rest: AsyncIterator[StreamEvent],
        attempted: Attempted,
        probe: CacheProbe | None,
    ) -> AsyncIterator[StreamEvent]:
        """Forward provider events to the client, after the response has been committed."""
        wants_usage = request.wants_stream_usage
        accumulated = StreamAccumulator()
        finished = False
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
            finished = True
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
            status = "ok" if finished else "stream_interrupted"
            cost = self._finish(call, attempted, usage, status=status)
            if finished and call.ctx.cache_store and not accumulated.has_tool_calls:
                completion = accumulated.to_completion(usage)
                if is_storable(completion):
                    self._remember(call.ctx, request, probe, completion, attempted.target, cost)
            await close_stream(rest)

    # ------------------------------------------------------------ caching

    async def _lookup(
        self, ctx: CallContext, request: ChatCompletionRequest, meta: ResponseMeta
    ) -> tuple[CacheHit | None, CacheProbe | None]:
        if self._cache is None:
            return None, None
        if not ctx.cache_lookup:
            meta.cache = CacheStatus.BYPASS
            return None, None
        return await self._cache.lookup(ctx.key.owner, request)

    def _remember(
        self,
        ctx: CallContext,
        request: ChatCompletionRequest,
        probe: CacheProbe | None,
        completion: Completion,
        target: TargetSpec,
        cost: Decimal,
    ) -> None:
        if self._cache is None:
            return
        entry = CachedCompletion(
            body=completion.body,
            usage=completion.usage,
            provider=target.provider,
            provider_model=target.model,
            cost_usd=cost,
        )
        self._background.spawn(
            self._cache.store(ctx.key.owner, request, probe, entry), name="cache-store"
        )

    def _served_from_cache(self, call: _Call, hit: CacheHit) -> None:
        meta = call.meta
        meta.cache = CacheStatus(hit.layer)
        meta.provider = hit.entry.provider
        meta.provider_model = hit.entry.provider_model
        if hit.similarity is not None:
            meta.extra_headers["x-relay-cache-similarity"] = str(hit.similarity)
        _record_request(call, "ok", seconds=self._clock() - call.started)
        metrics.CACHE_SAVINGS.labels(route=call.route.model).inc(float(hit.entry.cost_usd))
        # A cached answer costs nothing upstream: the request counts against the rate
        # limit, but its token reservation is refunded.
        self._settle(call.reservation, tokens=0, cost=Decimal(0))
        self._record_usage(call, "ok", Usage(), Decimal(0), saved=hit.entry.cost_usd)

    # ------------------------------------------------------------ accounting

    def _finish(
        self, call: _Call, attempted: Attempted, usage: Usage, *, status: str = "ok"
    ) -> Decimal:
        target = attempted.target
        cost = target.cost(usage.prompt_tokens, usage.completion_tokens)
        metrics.TOKENS.labels(provider=target.provider, model=target.model, kind="prompt").inc(
            usage.prompt_tokens
        )
        metrics.TOKENS.labels(provider=target.provider, model=target.model, kind="completion").inc(
            usage.completion_tokens
        )
        metrics.COST.labels(provider=target.provider, model=target.model).inc(float(cost))
        self._settle(call.reservation, tokens=usage.total_tokens, cost=cost)
        self._record_usage(call, status, usage, cost)
        return cost

    def _failed(self, call: _Call, exc: RelayError) -> None:
        _record_request(call, exc.code, seconds=self._clock() - call.started)
        # The request still counts against requests/minute, but its token reservation is
        # given back: nothing was generated.
        self._settle(call.reservation, tokens=0, cost=Decimal(0))
        self._record_usage(call, exc.code, Usage(), Decimal(0))

    def _settle(self, reservation: Reservation, *, tokens: int, cost: Decimal) -> None:
        self._background.spawn(self._settle_and_alert(reservation, tokens, cost), name="settle")

    async def _settle_and_alert(self, reservation: Reservation, tokens: int, cost: Decimal) -> None:
        update = await self._quotas.settle(reservation, tokens=tokens, cost_usd=cost)
        if update is not None and cost > 0 and self._alerts is not None:
            await self._alerts.check(update)

    def _record_usage(
        self,
        call: _Call,
        status: str,
        usage: Usage,
        cost: Decimal,
        *,
        saved: Decimal = Decimal(0),
    ) -> None:
        if self._usage is None:
            return
        meta = call.meta
        self._usage.record(
            UsageEvent(
                request_id=call.ctx.request_id,
                api_key_id=call.ctx.key.id,
                owner=call.ctx.key.owner,
                route=call.route.model,
                provider=meta.provider,
                provider_model=meta.provider_model,
                status=status,
                cache=meta.cache.value,
                stream=call.stream,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=cost,
                saved_usd=saved,
                latency_ms=round((self._clock() - call.started) * 1000),
                attempts=meta.attempts,
                replica=self._replica,
            )
        )


@dataclass(slots=True)
class _Call:
    """One request's state through the pipeline."""

    ctx: CallContext
    route: RouteSpec
    reservation: Reservation
    meta: ResponseMeta
    started: float
    stream: bool


async def _replay(chunks: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    for event in chunks:
        yield event


def _shape(event: StreamEvent, wants_usage: bool) -> StreamEvent | None:
    """Drop the usage the gateway asked for on the client's behalf, unless it asked too."""
    if wants_usage or "usage" not in event:
        return event
    if not event.get("choices"):
        return None
    return {key: value for key, value in event.items() if key != "usage"}


def _record_request(call: _Call, status: str, *, seconds: float) -> None:
    route, cache = call.route.model, call.meta.cache.value
    metrics.REQUESTS.labels(route=route, status=status, cache=cache).inc()
    metrics.REQUEST_DURATION.labels(
        route=route, stream=str(call.stream).lower(), cache=cache
    ).observe(seconds)


def _record_success(call: _Call, attempted: Attempted, *, seconds: float) -> None:
    _record_request(call, "ok", seconds=seconds)
    metrics.GATEWAY_OVERHEAD.labels(stream=str(call.stream).lower()).observe(
        max(0.0, seconds - attempted.upstream_s)
    )
    if attempted.fallback:
        metrics.FALLBACKS.labels(route=call.route.model, provider=attempted.target.provider).inc()
