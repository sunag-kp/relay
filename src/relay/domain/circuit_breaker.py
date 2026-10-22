"""Circuit breaker: stop sending traffic to a provider that keeps failing, then probe it.

    CLOSED --(failure rate >= threshold over the last N calls)--> OPEN
    OPEN --(open_seconds elapsed)--> HALF_OPEN
    HALF_OPEN --(all probe calls succeed)--> CLOSED
    HALF_OPEN --(any probe fails)--> OPEN

A count-based sliding window (the last `window_size` outcomes) rather than consecutive
failures, so a provider failing 60% of the time trips even if it never fails twice in a row.
`min_calls` stops a single early failure from tripping a cold breaker.

Pure and single-threaded: one instance per provider per gateway replica, driven from the
event loop, with an injectable clock for tests.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from relay.domain.catalog import BreakerSpec


class BreakerState(StrEnum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"

    @property
    def gauge_value(self) -> int:
        return {"closed": 0, "half_open": 1, "open": 2}[self.value]


@dataclass(frozen=True, slots=True)
class Permit:
    """Permission to make one call; `probe` marks a half-open trial call."""

    probe: bool


StateListener = Callable[[str, BreakerState, BreakerState], None]


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        spec: BreakerSpec,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_change: StateListener | None = None,
    ) -> None:
        self.name = name
        self.spec = spec
        self._clock = clock
        self._on_change = on_change
        self._state = BreakerState.CLOSED
        self._outcomes: deque[bool] = deque(maxlen=spec.window_size)  # True = failure
        self._opened_at = 0.0
        self._probes_in_flight = 0
        self._probe_successes = 0

    @property
    def state(self) -> BreakerState:
        if (
            self._state is BreakerState.OPEN
            and self._clock() - self._opened_at >= self.spec.open_seconds
        ):
            self._transition(BreakerState.HALF_OPEN)
        return self._state

    @property
    def failure_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return sum(self._outcomes) / len(self._outcomes)

    def retry_after_s(self) -> float:
        """Seconds until an open breaker lets a probe through (0 if not open)."""
        if self.state is not BreakerState.OPEN:
            return 0.0
        return max(0.0, self.spec.open_seconds - (self._clock() - self._opened_at))

    def try_acquire(self) -> Permit | None:
        state = self.state
        if state is BreakerState.CLOSED:
            return Permit(probe=False)
        if (
            state is BreakerState.HALF_OPEN
            and self._probes_in_flight < self.spec.half_open_max_calls
        ):
            self._probes_in_flight += 1
            return Permit(probe=True)
        return None

    def on_success(self, permit: Permit) -> None:
        if permit.probe:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            if self._state is BreakerState.HALF_OPEN:
                self._probe_successes += 1
                if self._probe_successes >= self.spec.half_open_max_calls:
                    self._transition(BreakerState.CLOSED)
            return
        if self._state is BreakerState.CLOSED:
            self._outcomes.append(False)

    def on_failure(self, permit: Permit) -> None:
        if permit.probe:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            if self._state is BreakerState.HALF_OPEN:
                self._transition(BreakerState.OPEN)
            return
        if self._state is not BreakerState.CLOSED:
            return
        self._outcomes.append(True)
        if (
            len(self._outcomes) >= self.spec.min_calls
            and self.failure_rate >= self.spec.failure_rate_threshold
        ):
            self._transition(BreakerState.OPEN)

    def release(self, permit: Permit) -> None:
        """The call ended without saying anything about provider health (the request was
        invalid, or the client went away): return a probe slot without recording."""
        if permit.probe:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)

    def _transition(self, to: BreakerState) -> None:
        previous, self._state = self._state, to
        if to is BreakerState.OPEN:
            self._opened_at = self._clock()
        if to is BreakerState.HALF_OPEN:
            self._probes_in_flight = 0
            self._probe_successes = 0
        if to is BreakerState.CLOSED:
            self._outcomes.clear()
        if self._on_change is not None and previous is not to:
            self._on_change(self.name, previous, to)
