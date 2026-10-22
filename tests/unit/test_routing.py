from __future__ import annotations

import random
from collections import Counter

import pytest

from relay.domain.catalog import RoutingPolicy, TargetSpec
from relay.domain.routing import Ewma, RoutingInputs, backoff_s, plan

A = TargetSpec(provider="a", model="a", weight=7, input_per_mtok=5, output_per_mtok=5)
B = TargetSpec(provider="b", model="b", weight=3, input_per_mtok=1, output_per_mtok=20)
C = TargetSpec(provider="c", model="c", weight=0, input_per_mtok=3, output_per_mtok=3)


def inputs(
    prompt: int = 100,
    completion: int = 100,
    latency: dict[str, float] | None = None,
    seed: int = 1,
    explore: float = 0.0,
) -> RoutingInputs:
    latency = latency or {}
    return RoutingInputs(
        prompt_tokens=prompt,
        completion_tokens=completion,
        latency_ms=lambda t: latency.get(t.provider),
        rng=random.Random(seed),
        exploration_rate=explore,
    )


def names(targets: list[TargetSpec]) -> list[str]:
    return [t.provider for t in targets]


def test_priority_keeps_configured_order() -> None:
    assert names(plan(RoutingPolicy.PRIORITY, [B, A, C], inputs())) == ["b", "a", "c"]


def test_weighted_split_matches_weights_and_zero_weight_is_fallback_only() -> None:
    rng_inputs = inputs(seed=42)
    firsts = Counter(
        plan(RoutingPolicy.WEIGHTED, [A, B, C], rng_inputs)[0].provider for _ in range(20_000)
    )
    assert firsts["c"] == 0
    assert firsts["a"] / 20_000 == pytest.approx(0.7, abs=0.02)
    # every plan still contains every target, so fallbacks exist
    assert sorted(names(plan(RoutingPolicy.WEIGHTED, [A, B, C], rng_inputs))) == ["a", "b", "c"]
    assert plan(RoutingPolicy.WEIGHTED, [A, B, C], rng_inputs)[-1] is C


def test_cheapest_depends_on_the_request_shape() -> None:
    # Long prompt, short answer: B's cheap input wins.
    assert names(plan(RoutingPolicy.CHEAPEST, [A, B, C], inputs(prompt=10_000, completion=10))) == [
        "b",
        "c",
        "a",
    ]
    # Short prompt, long answer: B's expensive output loses.
    assert names(plan(RoutingPolicy.CHEAPEST, [A, B, C], inputs(prompt=10, completion=10_000))) == [
        "c",
        "a",
        "b",
    ]


def test_lowest_latency_tries_unmeasured_targets_first_then_fastest() -> None:
    measured = inputs(latency={"a": 120.0, "b": 40.0})
    assert names(plan(RoutingPolicy.LOWEST_LATENCY, [A, B, C], measured)) == ["c", "b", "a"]
    all_measured = inputs(latency={"a": 120.0, "b": 40.0, "c": 80.0})
    assert names(plan(RoutingPolicy.LOWEST_LATENCY, [A, B, C], all_measured)) == ["b", "c", "a"]


def test_lowest_latency_explores() -> None:
    explore = inputs(latency={"a": 1.0, "b": 50.0, "c": 99.0}, explore=1.0, seed=3)
    firsts = Counter(
        plan(RoutingPolicy.LOWEST_LATENCY, [A, B, C], explore)[0].provider for _ in range(300)
    )
    assert set(firsts) == {"a", "b", "c"}


def test_ewma_weights_recent_samples() -> None:
    ewma = Ewma(alpha=0.5)
    assert ewma.observe(100) == 100
    assert ewma.observe(200) == 150
    assert ewma.observe(200) == 175
    assert ewma.samples == 3


def test_full_jitter_backoff_is_bounded() -> None:
    rng = random.Random(7)
    samples = [backoff_s(attempt, 0.1, 1.0, rng) for attempt in range(6) for _ in range(200)]
    assert all(0 <= s <= 1.0 for s in samples)
    first_attempt = [backoff_s(0, 0.1, 1.0, rng) for _ in range(200)]
    assert max(first_attempt) <= 0.1
    assert min(first_attempt) < 0.02  # jitter reaches all the way down
