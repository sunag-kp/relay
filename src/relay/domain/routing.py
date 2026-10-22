"""Routing policies: order a route's targets into an attempt plan.

The first target in the plan gets the request; the rest are fallbacks, tried in order when
an attempt fails or a target's breaker is open. Every policy returns *all* targets, so a
policy decides preference, never availability.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from relay.domain.catalog import RoutingPolicy, TargetSpec

LatencyLookup = Callable[[TargetSpec], float | None]


@dataclass(frozen=True, slots=True)
class RoutingInputs:
    prompt_tokens: int
    completion_tokens: int
    latency_ms: LatencyLookup
    rng: random.Random
    # lowest-latency: share of requests sent to a random target so that every target's
    # latency estimate stays fresh (a provider that was slow once is not starved forever).
    exploration_rate: float = 0.05


def plan(
    policy: RoutingPolicy, targets: Sequence[TargetSpec], inputs: RoutingInputs
) -> list[TargetSpec]:
    if policy is RoutingPolicy.PRIORITY:
        return list(targets)
    if policy is RoutingPolicy.WEIGHTED:
        return _weighted(targets, inputs.rng)
    if policy is RoutingPolicy.CHEAPEST:
        return sorted(targets, key=lambda t: t.cost(inputs.prompt_tokens, inputs.completion_tokens))
    return _lowest_latency(targets, inputs)


def _weighted(targets: Sequence[TargetSpec], rng: random.Random) -> list[TargetSpec]:
    """Weighted random order without replacement (Efraimidis-Spirakis): the first pick is
    proportional to weight, and so is each fallback among the remaining targets."""

    def key(target: TargetSpec) -> float:
        if target.weight <= 0:
            return -1.0  # zero-weight targets are fallbacks only
        return float(rng.random() ** (1.0 / target.weight))

    return sorted(targets, key=key, reverse=True)


def _lowest_latency(targets: Sequence[TargetSpec], inputs: RoutingInputs) -> list[TargetSpec]:
    if len(targets) > 1 and inputs.rng.random() < inputs.exploration_rate:
        shuffled = list(targets)
        inputs.rng.shuffle(shuffled)
        return shuffled

    def key(indexed: tuple[int, TargetSpec]) -> tuple[float, int]:
        index, target = indexed
        latency = inputs.latency_ms(target)
        # Unmeasured targets go first so they get measured; ties keep configured order.
        return (-1.0 if latency is None else latency, index)

    return [target for _, target in sorted(enumerate(targets), key=key)]


class Ewma:
    """Exponentially weighted moving average: recent samples count more, memory is O(1)."""

    def __init__(self, alpha: float = 0.2) -> None:
        self.alpha = alpha
        self.value: float | None = None
        self.samples = 0

    def observe(self, sample: float) -> float:
        self.samples += 1
        self.value = (
            sample if self.value is None else self.alpha * sample + (1 - self.alpha) * self.value
        )
        return self.value


def backoff_s(attempt: int, base_s: float, cap_s: float, rng: random.Random) -> float:
    """Exponential backoff with full jitter: uniform(0, min(cap, base * 2^attempt)).

    Full jitter spreads retries from many clients over the whole window, so a provider that
    blipped is not hit by a synchronized wave of retries the moment it recovers.
    """
    return rng.uniform(0.0, min(cap_s, base_s * (2**attempt)))
