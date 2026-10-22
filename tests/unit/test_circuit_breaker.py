from __future__ import annotations

from relay.domain.catalog import BreakerSpec
from relay.domain.circuit_breaker import BreakerState, CircuitBreaker, Permit


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


SPEC = BreakerSpec(
    failure_rate_threshold=0.5, window_size=10, min_calls=4, open_seconds=30, half_open_max_calls=2
)


def make(spec: BreakerSpec = SPEC) -> tuple[CircuitBreaker, Clock, list[tuple[str, str]]]:
    clock = Clock()
    changes: list[tuple[str, str]] = []
    breaker = CircuitBreaker(
        "p", spec, clock=clock, on_change=lambda _n, a, b: changes.append((a.value, b.value))
    )
    return breaker, clock, changes


def fail(breaker: CircuitBreaker, times: int = 1) -> None:
    for _ in range(times):
        permit = breaker.try_acquire()
        assert permit is not None
        breaker.on_failure(permit)


def succeed(breaker: CircuitBreaker, times: int = 1) -> None:
    for _ in range(times):
        permit = breaker.try_acquire()
        assert permit is not None
        breaker.on_success(permit)


def test_does_not_trip_before_min_calls() -> None:
    breaker, _, _ = make()
    fail(breaker, 3)
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failure_rate == 1.0


def test_trips_on_failure_rate_over_the_window_not_consecutive_failures() -> None:
    breaker, _, changes = make()
    # alternate: never two failures in a row, but a 50% failure rate
    for _ in range(2):
        succeed(breaker)
        fail(breaker)
    assert breaker.state is BreakerState.OPEN
    assert changes == [("closed", "open")]
    assert breaker.try_acquire() is None


def test_low_failure_rate_keeps_it_closed() -> None:
    breaker, _, _ = make()
    for _ in range(10):
        succeed(breaker, 3)
        fail(breaker)
    assert breaker.state is BreakerState.CLOSED


def test_window_forgets_old_outcomes() -> None:
    breaker, _, _ = make()
    fail(breaker, 3)
    succeed(breaker, 10)  # pushes the failures out of the 10-call window
    fail(breaker, 3)
    assert breaker.failure_rate == 0.3
    assert breaker.state is BreakerState.CLOSED


def test_open_then_half_open_then_closed_on_successful_probes() -> None:
    breaker, clock, changes = make()
    fail(breaker, 4)
    assert breaker.retry_after_s() == 30
    clock.now += 29.5
    assert breaker.state is BreakerState.OPEN
    assert 0 < breaker.retry_after_s() <= 0.5
    clock.now += 0.5
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.retry_after_s() == 0

    first, second = breaker.try_acquire(), breaker.try_acquire()
    assert first == second == Permit(probe=True)
    assert breaker.try_acquire() is None  # only two probes in flight
    breaker.on_success(first)  # type: ignore[arg-type]
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.on_success(second)  # type: ignore[arg-type]
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failure_rate == 0.0
    assert changes == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


def test_failed_probe_reopens() -> None:
    breaker, clock, _ = make()
    fail(breaker, 4)
    clock.now += 30
    probe = breaker.try_acquire()
    assert probe is not None
    breaker.on_failure(probe)
    assert breaker.state is BreakerState.OPEN
    assert breaker.retry_after_s() == 30


def test_released_probe_frees_its_slot_without_counting() -> None:
    breaker, clock, _ = make()
    fail(breaker, 4)
    clock.now += 30
    probes = [breaker.try_acquire(), breaker.try_acquire()]
    assert breaker.try_acquire() is None
    breaker.release(probes[0])  # type: ignore[arg-type]
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.try_acquire() is not None


def test_late_results_after_reopening_are_ignored() -> None:
    breaker, clock, _ = make()
    fail(breaker, 4)
    clock.now += 30
    slow, fast = breaker.try_acquire(), breaker.try_acquire()
    breaker.on_failure(fast)  # type: ignore[arg-type]
    breaker.on_success(slow)  # type: ignore[arg-type]  # arrives after the reopen
    assert breaker.state is BreakerState.OPEN
    # and a closed-path success while open records nothing
    breaker.on_success(Permit(probe=False))
    breaker.on_failure(Permit(probe=False))
    assert breaker.state is BreakerState.OPEN
