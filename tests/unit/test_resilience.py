from __future__ import annotations

import asyncio
import random

import pytest

from relay.domain.catalog import BreakerSpec, Catalog
from relay.domain.chat import ChatCompletionRequest
from relay.domain.circuit_breaker import BreakerState
from relay.domain.errors import (
    AllProvidersFailed,
    FailureKind,
    GatewayTimeout,
    NoProviderAvailable,
    ProviderFailure,
    UpstreamRejected,
)
from relay.services.resilience import ProviderHealth, ResilientExecutor
from tests.fakes import FakeProvider, make_completion


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def catalog(max_retries: int = 2, timeout_s: float = 5, **breaker: float) -> Catalog:
    spec = BreakerSpec(**{"min_calls": 3, "window_size": 5, "open_seconds": 10, **breaker})  # type: ignore[arg-type]
    provider = {
        "kind": "openai_compatible",
        "max_retries": max_retries,
        "backoff_base_s": 0.1,
        "backoff_max_s": 1.0,
        "timeout_s": timeout_s,
        "first_token_timeout_s": 1,
        "breaker": spec.model_dump(),
    }
    return Catalog.model_validate(
        {
            "providers": [
                {"name": "alpha", "base_url": "http://a", **provider},
                {"name": "beta", "base_url": "http://b", **provider},
            ],
            "routes": [
                {
                    "model": "m",
                    "targets": [
                        {"provider": "alpha", "model": "alpha-1"},
                        {"provider": "beta", "model": "beta-1"},
                    ],
                }
            ],
        }
    )


class Rig:
    def __init__(self, cat: Catalog | None = None, **providers: FakeProvider) -> None:
        self.catalog = cat or catalog()
        self.providers = {
            "alpha": providers.get("alpha", FakeProvider("alpha")),
            "beta": providers.get("beta", FakeProvider("beta")),
        }
        self.clock = Clock()
        self.sleeps: list[float] = []
        self.health = ProviderHealth(self.catalog, clock=self.clock)

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.clock.now += seconds

        self.executor = ResilientExecutor(
            self.catalog,
            self.providers,
            self.health,
            rng=random.Random(0),
            clock=self.clock,
            sleep=sleep,
        )
        self.plan = list(self.catalog.routes[0].targets)

    async def complete(self, deadline: float = 100.0):  # type: ignore[no-untyped-def]
        return await self.executor.complete(self.plan, REQUEST, deadline)


REQUEST = ChatCompletionRequest.model_validate(
    {"model": "m", "messages": [{"role": "user", "content": "x"}]}
)


def server_error(provider: str = "alpha") -> ProviderFailure:
    return ProviderFailure(provider, FailureKind.SERVER, "503", status=503)


async def test_transient_failure_is_retried_with_backoff_on_the_same_provider() -> None:
    rig = Rig(alpha=FakeProvider("alpha", behaviours=[server_error(), make_completion("ok")]))
    completion, attempted = await rig.complete()
    assert completion.text() == "ok"
    assert attempted.target.provider == "alpha"
    assert attempted.attempts == 2
    assert not attempted.fallback
    assert len(rig.sleeps) == 1
    assert 0 <= rig.sleeps[0] <= 0.1


async def test_retries_exhausted_then_falls_back_to_the_next_provider() -> None:
    rig = Rig(alpha=FakeProvider("alpha", behaviours=[server_error()]))
    _completion, attempted = await rig.complete()
    assert attempted.target.provider == "beta"
    assert attempted.attempts == 4  # 1 + 2 retries on alpha, then beta
    assert attempted.fallback
    assert rig.providers["alpha"].calls == ["complete:alpha-1"] * 3


async def test_auth_failure_falls_back_without_retrying() -> None:
    rig = Rig(
        alpha=FakeProvider(
            "alpha", behaviours=[ProviderFailure.from_status("alpha", 401, "bad key")]
        )
    )
    _, attempted = await rig.complete()
    assert attempted.target.provider == "beta"
    assert attempted.attempts == 2
    assert rig.sleeps == []


async def test_invalid_request_stops_without_fallback_or_breaker_damage() -> None:
    rig = Rig(
        alpha=FakeProvider("alpha", behaviours=[ProviderFailure.from_status("alpha", 400, "bad")])
    )
    for _ in range(10):
        with pytest.raises(UpstreamRejected):
            await rig.complete()
    assert rig.providers["beta"].calls == []
    assert rig.health.breaker("alpha").state is BreakerState.CLOSED


async def test_long_retry_after_skips_to_the_next_provider() -> None:
    throttled = ProviderFailure.from_status("alpha", 429, "slow down", retry_after_s=30)
    rig = Rig(alpha=FakeProvider("alpha", behaviours=[throttled]))
    _, attempted = await rig.complete()
    assert attempted.target.provider == "beta"
    assert rig.sleeps == []


async def test_short_retry_after_is_honoured() -> None:
    throttled = ProviderFailure.from_status("alpha", 429, "slow down", retry_after_s=0.5)
    rig = Rig(alpha=FakeProvider("alpha", behaviours=[throttled, make_completion("ok")]))
    _, attempted = await rig.complete()
    assert attempted.target.provider == "alpha"
    assert rig.sleeps == [0.5]


async def test_breaker_opens_and_later_requests_skip_the_provider() -> None:
    rig = Rig(catalog(max_retries=0), alpha=FakeProvider("alpha", behaviours=[server_error()]))
    for _ in range(3):
        await rig.complete()
    assert rig.health.breaker("alpha").state is BreakerState.OPEN
    alpha_calls = len(rig.providers["alpha"].calls)

    _, attempted = await rig.complete()
    assert attempted.target.provider == "beta"
    assert attempted.attempts == 1  # alpha was skipped without a call
    assert len(rig.providers["alpha"].calls) == alpha_calls

    # After open_seconds a probe goes to alpha again; it has recovered, so it closes.
    rig.providers["alpha"].behaviours = [make_completion("back")]
    rig.clock.now += 10
    _completion, attempted = await rig.complete()
    assert attempted.target.provider == "alpha"
    assert rig.health.breaker("alpha").state is BreakerState.HALF_OPEN  # needs 2 good probes
    await rig.complete()
    assert rig.health.breaker("alpha").state is BreakerState.CLOSED


async def test_all_breakers_open_fails_fast_with_retry_after() -> None:
    rig = Rig(
        catalog(max_retries=0),
        alpha=FakeProvider("alpha", behaviours=[server_error()]),
        beta=FakeProvider("beta", behaviours=[server_error("beta")]),
    )
    for _ in range(3):
        with pytest.raises(AllProvidersFailed):
            await rig.complete()
    rig.clock.now += 4
    with pytest.raises(NoProviderAvailable) as info:
        await rig.complete()
    assert info.value.status_code == 503
    assert info.value.retry_after_s == pytest.approx(6)


async def test_deadline_stops_retries() -> None:
    rig = Rig(alpha=FakeProvider("alpha", behaviours=[server_error()]))
    rig.clock.now = 50
    with pytest.raises(GatewayTimeout):
        await rig.complete(deadline=50)


def _real_time_executor(cat: Catalog, alpha_delay_s: float) -> ResilientExecutor:
    providers = {
        "alpha": FakeProvider("alpha", delay_s=alpha_delay_s),
        "beta": FakeProvider("beta"),
    }
    loop = asyncio.get_running_loop()
    return ResilientExecutor(cat, providers, ProviderHealth(cat, clock=loop.time), clock=loop.time)


async def test_slow_attempt_times_out_and_falls_back() -> None:
    cat = catalog(max_retries=0, timeout_s=0.1)
    executor = _real_time_executor(cat, alpha_delay_s=5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    _, attempted = await executor.complete(list(cat.routes[0].targets), REQUEST, loop.time() + 2)
    assert attempted.target.provider == "beta"
    assert loop.time() - started < 0.5


async def test_slow_attempt_that_uses_up_the_deadline_is_a_504() -> None:
    cat = catalog(max_retries=0, timeout_s=5)
    executor = _real_time_executor(cat, alpha_delay_s=5)
    loop = asyncio.get_running_loop()
    with pytest.raises(GatewayTimeout):
        await executor.complete(list(cat.routes[0].targets), REQUEST, loop.time() + 0.1)


async def test_stream_that_fails_to_start_falls_back() -> None:
    rig = Rig(alpha=FakeProvider("alpha", behaviours=[server_error()]))
    opened = await rig.executor.open_stream(
        rig.plan, REQUEST.model_copy(update={"stream": True}), 100
    )
    assert opened.attempted.target.provider == "beta"
    assert opened.first["choices"][0]["delta"]["role"] == "assistant"
    rest = [event async for event in opened.rest]
    assert rest[-1]["usage"]["completion_tokens"] == 4


async def test_cancellation_releases_a_half_open_probe() -> None:
    rig = Rig(catalog(max_retries=0), alpha=FakeProvider("alpha", behaviours=[server_error()]))
    for _ in range(3):
        await rig.complete()
    rig.clock.now += 10
    rig.providers["alpha"].behaviours = [lambda: (_ for _ in ()).throw(asyncio.CancelledError())]
    with pytest.raises(asyncio.CancelledError):
        await rig.complete()
    breaker = rig.health.breaker("alpha")
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.try_acquire() is not None
    assert breaker.try_acquire() is not None  # both probe slots are free again


def test_health_snapshot_reports_state_and_latency() -> None:
    rig = Rig()
    rig.health.observe_latency(rig.plan[0], stream=False, seconds=0.05)
    snapshot = rig.health.snapshot()
    assert snapshot["providers"]["alpha"]["state"] == "closed"
    assert snapshot["latency_ms"]["alpha/alpha-1"] == 50.0
