"""Calling providers reliably: timeouts, retries with jitter, circuit breakers and fallback.

The executor walks an attempt plan (targets in policy order). For each target it asks the
provider's breaker for a permit, calls with a timeout bounded by the request deadline, and
on a transient failure retries with backoff before falling through to the next target.
Requests the provider rejects as invalid stop immediately: another provider would reject
them too, and they say nothing about provider health.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from opentelemetry import trace

from relay.domain.catalog import Catalog, ProviderSpec, TargetSpec
from relay.domain.chat import ChatCompletionRequest, Completion
from relay.domain.circuit_breaker import BreakerState, CircuitBreaker, Permit
from relay.domain.errors import (
    AllProvidersFailed,
    FailureKind,
    GatewayTimeout,
    NoProviderAvailable,
    ProviderFailure,
    UpstreamRejected,
)
from relay.domain.routing import Ewma, backoff_s
from relay.observability import metrics
from relay.observability.log import get_logger
from relay.services.ports import ChatProvider, StreamEvent

log = get_logger("relay.resilience")
tracer = trace.get_tracer("relay.resilience")

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class ProviderHealth:
    """This replica's view of provider health: a breaker per provider, latency per target.

    Deliberately per-replica (like Envoy's outlier detection): breaker decisions need no
    network hop and keep working when Redis is down. Each replica learns about a failing
    provider from its own traffic, which takes a few requests per replica.
    """

    def __init__(self, catalog: Catalog, *, clock: Clock = time.monotonic) -> None:
        self._breakers = {
            spec.name: CircuitBreaker(spec.name, spec.breaker, clock=clock, on_change=_on_change)
            for spec in catalog.providers
        }
        self._latency: dict[tuple[str, bool], Ewma] = {}
        for name in self._breakers:
            metrics.BREAKER_STATE.labels(provider=name).set(0)

    def breaker(self, provider: str) -> CircuitBreaker:
        return self._breakers[provider]

    def latency_ms(self, target: TargetSpec, *, stream: bool) -> float | None:
        ewma = self._latency.get((target.key, stream))
        return ewma.value if ewma else None

    def observe_latency(self, target: TargetSpec, *, stream: bool, seconds: float) -> None:
        self._latency.setdefault((target.key, stream), Ewma()).observe(seconds * 1000)

    def snapshot(self) -> dict[str, Any]:
        return {
            "providers": {
                name: {
                    "state": breaker.state.value,
                    "failure_rate": round(breaker.failure_rate, 3),
                    "retry_after_s": round(breaker.retry_after_s(), 1),
                }
                for name, breaker in self._breakers.items()
            },
            "latency_ms": {
                f"{key}{' (stream)' if stream else ''}": round(ewma.value or 0.0, 1)
                for (key, stream), ewma in self._latency.items()
            },
        }


def _on_change(provider: str, previous: BreakerState, current: BreakerState) -> None:
    metrics.BREAKER_STATE.labels(provider=provider).set(current.gauge_value)
    metrics.BREAKER_TRANSITIONS.labels(provider=provider, to=current.value).inc()
    log.warning("circuit_breaker_transition", provider=provider, previous=previous, current=current)


@dataclass(slots=True)
class Attempted:
    """Which target served the request, after how many attempts."""

    target: TargetSpec
    attempts: int
    upstream_s: float  # time spent waiting on providers (for gateway-overhead accounting)
    fallback: bool


@dataclass(slots=True)
class _Outcome[T]:
    elapsed: float
    result: T | None = None
    failure: ProviderFailure | None = None


@dataclass(slots=True)
class OpenedStream:
    first: StreamEvent
    rest: AsyncIterator[StreamEvent]
    attempted: Attempted


class ResilientExecutor:
    def __init__(
        self,
        catalog: Catalog,
        providers: Mapping[str, ChatProvider],
        health: ProviderHealth,
        *,
        rng: random.Random | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._catalog = catalog
        self._providers = providers
        self._health = health
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
        self._clock = clock
        self._sleep = sleep

    async def complete(
        self, plan: Sequence[TargetSpec], request: ChatCompletionRequest, deadline: float
    ) -> tuple[Completion, Attempted]:
        async def call(target: TargetSpec, timeout_s: float) -> Completion:
            async with asyncio.timeout(timeout_s):
                return await self._providers[target.provider].complete(request, target.model)

        return await self._run(plan, request, deadline, call, stream=False)

    async def open_stream(
        self, plan: Sequence[TargetSpec], request: ChatCompletionRequest, deadline: float
    ) -> OpenedStream:
        async def call(
            target: TargetSpec, timeout_s: float
        ) -> tuple[StreamEvent, AsyncIterator[StreamEvent]]:
            provider = self._providers[target.provider]
            spec = self._catalog.provider(target.provider)
            async with asyncio.timeout(min(timeout_s, spec.first_token_timeout_s)):
                return await open_stream(provider, request, target.model)

        (first, rest), attempted = await self._run(plan, request, deadline, call, stream=True)
        return OpenedStream(first=first, rest=rest, attempted=attempted)

    def record_stream_failure(self, provider: str) -> None:
        """A stream that started fine broke mid-way: count it against the provider."""
        self._health.breaker(provider).on_failure(Permit(probe=False))
        metrics.UPSTREAM_ATTEMPTS.labels(provider=provider, outcome="midstream").inc()

    async def _run[T](
        self,
        plan: Sequence[TargetSpec],
        request: ChatCompletionRequest,
        deadline: float,
        call: Callable[[TargetSpec, float], Awaitable[T]],
        *,
        stream: bool,
    ) -> tuple[T, Attempted]:
        attempts = 0
        upstream_s = 0.0
        last_failure: ProviderFailure | None = None
        open_breakers: list[CircuitBreaker] = []

        for position, target in enumerate(plan):
            spec = self._catalog.provider(target.provider)
            breaker = self._health.breaker(target.provider)
            for retry in range(spec.max_retries + 1):
                permit = breaker.try_acquire()
                if permit is None:
                    open_breakers.append(breaker)
                    break
                remaining = deadline - self._clock()
                if remaining <= 0:
                    breaker.release(permit)
                    raise GatewayTimeout("The request deadline passed before a provider answered.")
                attempts += 1
                if retry:
                    metrics.RETRIES.labels(provider=target.provider).inc()
                outcome = await self._attempt(
                    target, permit, call, min(spec.timeout_s, remaining), attempts
                )
                upstream_s += outcome.elapsed
                if outcome.failure is None:
                    self._succeeded(target, permit, outcome.elapsed, stream=stream)
                    attempted = Attempted(target, attempts, upstream_s, fallback=position > 0)
                    return cast("T", outcome.result), attempted
                last_failure = outcome.failure
                self._failed(target, permit, last_failure, attempts)
                delay = self._retry_delay(last_failure, retry, spec)
                if delay is None or self._clock() + delay >= deadline:
                    break
                await self._sleep(delay)

        raise self._exhausted(attempts, last_failure, open_breakers)

    async def _attempt[T](
        self,
        target: TargetSpec,
        permit: Permit,
        call: Callable[[TargetSpec, float], Awaitable[T]],
        timeout_s: float,
        attempt: int,
    ) -> _Outcome[T]:
        started = self._clock()
        with tracer.start_as_current_span(
            "relay.upstream_attempt",
            attributes={
                "relay.provider": target.provider,
                "relay.model": target.model,
                "relay.attempt": attempt,
                "relay.breaker_probe": permit.probe,
            },
        ) as span:
            try:
                result = await call(target, timeout_s)
            except ProviderFailure as failure:
                span.set_attribute("relay.failure", failure.kind.value)
                return _Outcome(self._clock() - started, failure=failure)
            except TimeoutError:
                span.set_attribute("relay.failure", FailureKind.TIMEOUT.value)
                timeout = ProviderFailure(target.provider, FailureKind.TIMEOUT, "attempt timed out")
                return _Outcome(self._clock() - started, failure=timeout)
            except BaseException:
                self._health.breaker(target.provider).release(permit)  # e.g. client went away
                raise
            return _Outcome(self._clock() - started, result=result)

    def _succeeded(
        self, target: TargetSpec, permit: Permit, elapsed: float, *, stream: bool
    ) -> None:
        self._health.breaker(target.provider).on_success(permit)
        self._health.observe_latency(target, stream=stream, seconds=elapsed)
        metrics.UPSTREAM_ATTEMPTS.labels(provider=target.provider, outcome="ok").inc()
        metrics.UPSTREAM_DURATION.labels(provider=target.provider, model=target.model).observe(
            elapsed
        )

    def _failed(
        self, target: TargetSpec, permit: Permit, failure: ProviderFailure, attempt: int
    ) -> None:
        breaker = self._health.breaker(target.provider)
        metrics.UPSTREAM_ATTEMPTS.labels(provider=target.provider, outcome=failure.kind.value).inc()
        if failure.is_client_error:
            breaker.release(permit)
            raise UpstreamRejected(
                f"{target.provider} rejected the request: {failure.message}"
            ) from failure
        breaker.on_failure(permit)
        log.info(
            "upstream_attempt_failed",
            provider=target.provider,
            model=target.model,
            attempt=attempt,
            failure=failure.kind.value,
            status=failure.status,
            error=failure.message[:200],
        )

    @staticmethod
    def _exhausted(
        attempts: int, last: ProviderFailure | None, open_breakers: list[CircuitBreaker]
    ) -> Exception:
        if attempts == 0 and open_breakers:
            return NoProviderAvailable(
                "Every provider for this model is temporarily disabled by its circuit breaker.",
                retry_after_s=min(b.retry_after_s() for b in open_breakers),
            )
        detail = f"{last.provider}: {last.kind}: {last.message}" if last else "none"
        return AllProvidersFailed(
            f"All {attempts} attempt(s) across the route's providers failed (last: {detail})."
        )

    def _retry_delay(
        self, failure: ProviderFailure, retry: int, spec: ProviderSpec
    ) -> float | None:
        """Seconds to wait before retrying the same target, or None to move on."""
        if not failure.retryable or retry >= spec.max_retries:
            return None
        if failure.retry_after_s is not None:
            # Honour the provider's Retry-After, unless it is longer than we would ever back
            # off: then another provider is the faster way to an answer.
            return failure.retry_after_s if failure.retry_after_s <= spec.backoff_max_s else None
        return backoff_s(retry, spec.backoff_base_s, spec.backoff_max_s, self._rng)


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
        await close_stream(events)
        raise
    return first, events


async def close_stream(events: AsyncIterator[StreamEvent]) -> None:
    close = getattr(events, "aclose", None)
    if close is not None:
        await close()
