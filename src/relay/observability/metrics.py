"""Prometheus metrics. Every replica exposes its own `/metrics`; Prometheus scrapes each one
and the dashboards aggregate across replicas.

Labels stay low-cardinality on purpose (route, provider, model, outcome): per-API-key
numbers live in Postgres usage tables, not in Prometheus, where one series per key would
grow without bound.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
OVERHEAD_BUCKETS = (0.0005, 0.001, 0.002, 0.003, 0.005, 0.0075, 0.01, 0.015, 0.025, 0.05, 0.1)

REQUESTS = Counter(
    "relay_requests_total",
    "Chat completion requests by route, outcome and cache result.",
    ["route", "status", "cache"],
)
REQUEST_DURATION = Histogram(
    "relay_request_duration_seconds",
    "End-to-end gateway latency (to the last byte for non-streamed, first byte for streamed).",
    ["route", "stream", "cache"],
    buckets=LATENCY_BUCKETS,
)
GATEWAY_OVERHEAD = Histogram(
    "relay_gateway_overhead_seconds",
    "Time spent in the gateway itself: request duration minus time waiting on providers.",
    ["stream"],
    buckets=OVERHEAD_BUCKETS,
)
TIME_TO_FIRST_TOKEN = Histogram(
    "relay_time_to_first_token_seconds",
    "Streaming: time from request to the first upstream event.",
    ["route"],
    buckets=LATENCY_BUCKETS,
)
UPSTREAM_ATTEMPTS = Counter(
    "relay_upstream_attempts_total",
    "Calls to providers by outcome (ok, timeout, server, rate_limited, ...).",
    ["provider", "outcome"],
)
UPSTREAM_DURATION = Histogram(
    "relay_upstream_duration_seconds",
    "Provider latency per attempt (non-streamed: full response; streamed: first event).",
    ["provider", "model"],
    buckets=LATENCY_BUCKETS,
)
RETRIES = Counter("relay_retries_total", "Retries on the same provider.", ["provider"])
FALLBACKS = Counter(
    "relay_fallbacks_total",
    "Requests served by a target other than the plan's first choice.",
    ["route", "provider"],
)
BREAKER_STATE = Gauge(
    "relay_circuit_breaker_state",
    "Circuit breaker state per provider on this replica: 0 closed, 1 half-open, 2 open.",
    ["provider"],
)
BREAKER_TRANSITIONS = Counter(
    "relay_circuit_breaker_transitions_total",
    "Circuit breaker state changes.",
    ["provider", "to"],
)
RATE_LIMITED = Counter(
    "relay_rate_limited_total",
    "Requests rejected with 429, by reason (requests, tokens, budget).",
    ["reason"],
)
RATE_LIMITER_ERRORS = Counter(
    "relay_rate_limiter_errors_total",
    "Admissions allowed without a limit check because the limiter store was unavailable.",
)
CACHE_LOOKUPS = Counter(
    "relay_cache_lookups_total",
    "Cache lookups by layer (exact, semantic) and result (hit, miss).",
    ["layer", "result"],
)
TOKENS = Counter(
    "relay_tokens_total",
    "Tokens processed by providers (cache hits excluded).",
    ["provider", "model", "kind"],
)
COST = Counter("relay_cost_usd_total", "Estimated provider spend in USD.", ["provider", "model"])
CACHE_SAVINGS = Counter(
    "relay_cache_savings_usd_total", "Provider spend avoided by cache hits.", ["route"]
)
USAGE_QUEUE_DEPTH = Gauge("relay_usage_queue_depth", "Usage events waiting to be written.")
USAGE_DROPPED = Counter("relay_usage_events_dropped_total", "Usage events dropped (queue full).")
