"""The Lua limiter against a real Redis: atomic under concurrency, refills over time."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from redis.asyncio import Redis

from relay.domain.limits import LimitCheck, LimitReason
from relay.infra.redis.rate_limiter import RedisRateLimiter

pytestmark = pytest.mark.integration


def check(
    key_id: str, *, rpm: int = 60, tpd: int = 100_000, tokens: int = 10, budget: int | None = None
) -> LimitCheck:
    return LimitCheck(
        key_id=key_id,
        rpm_limit=rpm,
        tpd_limit=tpd,
        tokens=tokens,
        budget_micro_usd=budget,
        period="2026-10",
        seconds_to_period_end=3600,
    )


async def test_concurrent_admissions_never_exceed_the_limit(redis: Redis) -> None:
    limiter = RedisRateLimiter(redis, namespace="relay-it")
    key = uuid.uuid4().hex
    results = await asyncio.gather(*(limiter.admit(check(key, rpm=50)) for _ in range(300)))
    allowed = sum(r.allowed for r in results)
    assert allowed == 50
    denied = [r for r in results if not r.allowed]
    assert {r.reason for r in denied} == {LimitReason.REQUESTS}
    # an empty bucket refills one request every 60/50 = 1.2 s
    assert all(0 < r.retry_after_s <= 1.2 for r in denied)


async def test_bucket_refills_with_time(redis: Redis) -> None:
    limiter = RedisRateLimiter(redis, namespace="relay-it")
    key = uuid.uuid4().hex
    fast = check(key, rpm=600, tokens=1)  # refills 10 requests per second
    while (await limiter.admit(fast)).allowed:
        pass
    await asyncio.sleep(0.35)
    allowed = 0
    while (await limiter.admit(fast)).allowed:
        allowed += 1
    assert 3 <= allowed <= 5


async def test_tokens_are_reserved_then_settled(redis: Redis) -> None:
    limiter = RedisRateLimiter(redis, namespace="relay-it")
    key = uuid.uuid4().hex
    reserve = check(key, tpd=1000, tokens=600)
    first = await limiter.admit(reserve)
    assert first.allowed
    assert first.remaining_tokens == 400
    # A second 600-token reservation does not fit...
    second = await limiter.admit(reserve)
    assert not second.allowed
    assert second.reason is LimitReason.TOKENS
    # ...until the first one settles at its real usage (100 tokens), refunding 500.
    remaining, spend = await limiter.settle(reserve, actual_tokens=100, spend_micro_usd=250)
    assert remaining == 900
    assert spend == 250
    assert (await limiter.admit(reserve)).allowed


async def test_overrun_becomes_debt(redis: Redis) -> None:
    limiter = RedisRateLimiter(redis, namespace="relay-it")
    key = uuid.uuid4().hex
    reserve = check(key, tpd=1000, tokens=500)
    assert (await limiter.admit(reserve)).allowed
    remaining, _ = await limiter.settle(reserve, actual_tokens=1400, spend_micro_usd=0)
    assert remaining == 0  # the bucket is now at -400
    small = await limiter.admit(check(key, tpd=1000, tokens=1))
    assert not small.allowed
    assert small.retry_after_s > 400 * 86400 / 1000 * 0.99


async def test_enforced_budget_blocks_once_spent(redis: Redis) -> None:
    limiter = RedisRateLimiter(redis, namespace="relay-it")
    key = uuid.uuid4().hex
    with_budget = check(key, budget=1000)
    assert (await limiter.admit(with_budget)).allowed
    await limiter.settle(with_budget, actual_tokens=10, spend_micro_usd=1000)
    denied = await limiter.admit(with_budget)
    assert not denied.allowed
    assert denied.reason is LimitReason.BUDGET
    assert denied.retry_after_s == 3600
    # without an enforced budget the same spend does not block
    assert (await limiter.admit(check(key))).allowed


async def test_redis_outage_raises_dependency_unavailable() -> None:
    from relay.domain.errors import DependencyUnavailable
    from relay.infra.redis.client import create_redis

    broken = create_redis("redis://localhost:1/0")
    limiter = RedisRateLimiter(broken, namespace="relay-it")
    started = time.perf_counter()
    with pytest.raises(DependencyUnavailable):
        await limiter.admit(check("x"))
    assert time.perf_counter() - started < 5
    await broken.aclose()
