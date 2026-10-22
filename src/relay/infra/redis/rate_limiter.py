"""Distributed rate limits and quotas in Redis, made atomic with Lua.

Two token buckets per API key (requests/minute and tokens/day) plus a monthly spend counter.
Admission is one script: it refills both buckets, checks all three limits and consumes from
both buckets only if every check passes, so concurrent requests on any number of gateway
replicas can never overshoot a limit and a denied request consumes nothing.

Buckets refill with Redis server time (`TIME`), not the replica's clock, so replicas with
drifting clocks still agree. All keys of one API key share a hash tag, so the scripts also
work on Redis Cluster.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError

from relay.domain.errors import DependencyUnavailable
from relay.domain.limits import Admission, LimitCheck, LimitReason

# Shared by both scripts: refill a bucket to "now" and return its level.
_BUCKET = """
local function now_ms()
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end

local function level(key, capacity, window_ms, now)
  local state = redis.call('HMGET', key, 'v', 't')
  local value, ts = tonumber(state[1]), tonumber(state[2])
  if value == nil then return capacity end
  local refilled = value + math.max(0, now - ts) * capacity / window_ms
  if refilled > capacity then return capacity end
  return refilled
end
"""

ADMIT = (
    _BUCKET
    + """
-- KEYS: requests bucket, tokens bucket, spend counter
-- ARGV: rpm limit, tokens/day limit, token cost, budget (micro-USD, -1 = none), budget reset (s)
local now = now_ms()
local rpm_cap, tpd_cap, cost = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local rpm = level(KEYS[1], rpm_cap, 60000, now)
local tpd = level(KEYS[2], tpd_cap, 86400000, now)

if rpm < 1 then
  return {0, 'requests', tostring((1 - rpm) * 60 / rpm_cap), tostring(rpm), tostring(tpd)}
end
if tpd < cost then
  return {0, 'tokens', tostring((cost - tpd) * 86400 / tpd_cap), tostring(rpm), tostring(tpd)}
end
local budget = tonumber(ARGV[4])
if budget >= 0 and tonumber(redis.call('GET', KEYS[3]) or '0') >= budget then
  return {0, 'budget', ARGV[5], tostring(rpm), tostring(tpd)}
end

rpm = rpm - 1
tpd = tpd - cost
redis.call('HSET', KEYS[1], 'v', rpm, 't', now)
redis.call('PEXPIRE', KEYS[1], 120000)
redis.call('HSET', KEYS[2], 'v', tpd, 't', now)
redis.call('PEXPIRE', KEYS[2], 172800000)
return {1, 'ok', '0', tostring(rpm), tostring(tpd)}
"""
)

SETTLE = (
    _BUCKET
    + """
-- KEYS: tokens bucket, spend counter
-- ARGV: tokens/day limit, delta (actual - reserved tokens), spend to add (micro-USD), spend TTL (s)
local now = now_ms()
local tpd_cap, delta, spend = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
-- A refund cannot overfill the bucket; an overrun may push it below zero (debt), which
-- later admissions repay before they are allowed.
local tpd = math.min(tpd_cap, level(KEYS[1], tpd_cap, 86400000, now) - delta)
redis.call('HSET', KEYS[1], 'v', tpd, 't', now)
redis.call('PEXPIRE', KEYS[1], 172800000)
local total
if spend > 0 then
  total = redis.call('INCRBY', KEYS[2], spend)
  redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]))
else
  total = tonumber(redis.call('GET', KEYS[2]) or '0')
end
return {tostring(tpd), total}
"""
)

SPEND_TTL_S = 40 * 86400  # a budget period plus slack


class RedisRateLimiter:
    def __init__(self, redis: Redis, namespace: str = "relay") -> None:
        self._redis = redis
        self._namespace = namespace
        self._admit = redis.register_script(ADMIT)
        self._settle = redis.register_script(SETTLE)

    def _keys(self, key_id: str, period: str) -> tuple[str, str, str]:
        tag = f"{self._namespace}:{{{key_id}}}"
        return f"{tag}:rpm", f"{tag}:tpd", f"{tag}:spend:{period}"

    async def admit(self, check: LimitCheck) -> Admission:
        rpm_key, tpd_key, spend_key = self._keys(check.key_id, check.period)
        try:
            result = await self._admit(
                keys=[rpm_key, tpd_key, spend_key],
                args=[
                    check.rpm_limit,
                    check.tpd_limit,
                    check.tokens,
                    -1 if check.budget_micro_usd is None else check.budget_micro_usd,
                    check.seconds_to_period_end,
                ],
            )
        except RedisError as exc:
            raise DependencyUnavailable(f"rate limiter unavailable: {exc}") from exc
        allowed, reason, retry_after, rpm, tpd = (_text(part) for part in result)
        return Admission(
            allowed=allowed == "1",
            reason=None if reason == "ok" else LimitReason(reason),
            retry_after_s=float(retry_after),
            remaining_requests=max(0, int(float(rpm))),
            remaining_tokens=max(0, int(float(tpd))),
        )

    async def settle(
        self, check: LimitCheck, actual_tokens: int, spend_micro_usd: int
    ) -> tuple[int, int]:
        """Correct the token reservation and add spend; returns (remaining tokens, spend)."""
        _, tpd_key, spend_key = self._keys(check.key_id, check.period)
        try:
            remaining, total = await self._settle(
                keys=[tpd_key, spend_key],
                args=[check.tpd_limit, actual_tokens - check.tokens, spend_micro_usd, SPEND_TTL_S],
            )
        except RedisError as exc:
            raise DependencyUnavailable(f"rate limiter unavailable: {exc}") from exc
        return max(0, int(float(_text(remaining)))), int(total)


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
