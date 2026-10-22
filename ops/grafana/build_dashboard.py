"""Generates ops/grafana/dashboards/relay.json (run after editing the panels below).

python ops/grafana/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DS = {"type": "prometheus", "uid": "prometheus"}
OUT = Path(__file__).parent / "dashboards" / "relay.json"


def target(expr: str, legend: str = "") -> dict[str, Any]:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": legend or "A"}


def stat(title: str, expr: str, unit: str, x: int, y: int, *, decimals: int = 2) -> dict[str, Any]:
    return {
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": 4, "w": 4, "x": x, "y": y},
        "targets": [target(expr)],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals}, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "none",
            "graphMode": "area",
        },
    }


def series(
    title: str,
    targets: list[dict[str, Any]],
    unit: str,
    x: int,
    y: int,
    *,
    stack: bool = False,
    w: int = 12,
) -> dict[str, Any]:
    return {
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": 8, "w": w, "x": x, "y": y},
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": 10 if stack else 0,
                    "stacking": {"mode": "normal" if stack else "none"},
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi"},
        },
    }


def quantiles(metric: str, selector: str = "") -> list[dict[str, Any]]:
    return [
        target(
            f"histogram_quantile({q}, sum by (le) (rate({metric}_bucket{selector}[1m])))",
            f"p{int(q * 100)}",
        )
        for q in (0.5, 0.95, 0.99)
    ]


panels = [
    stat("Requests / s", "sum(rate(relay_requests_total[1m]))", "reqps", 0, 0),
    stat(
        "Error ratio",
        'sum(rate(relay_requests_total{status!="ok"}[5m])) / clamp_min(sum(rate(relay_requests_total[5m])), 1e-9)',
        "percentunit",
        4,
        0,
    ),
    stat(
        "Cache hit rate",
        'sum(rate(relay_cache_lookups_total{result="hit"}[5m])) / clamp_min(sum(rate(relay_cache_lookups_total{layer="exact"}[5m])), 1e-9)',
        "percentunit",
        8,
        0,
    ),
    stat(
        "Gateway overhead p95",
        "histogram_quantile(0.95, sum by (le) (rate(relay_gateway_overhead_seconds_bucket[5m])))",
        "s",
        12,
        0,
        decimals=4,
    ),
    stat("429 / s", "sum(rate(relay_rate_limited_total[1m]))", "reqps", 16, 0),
    stat(
        "Spend / hour",
        "sum(rate(relay_cost_usd_total[5m])) * 3600",
        "currencyUSD",
        20,
        0,
        decimals=4,
    ),
    series(
        "Requests / s by outcome",
        [target("sum by (status) (rate(relay_requests_total[1m]))", "{{status}}")],
        "reqps",
        0,
        4,
        stack=True,
    ),
    series(
        "End-to-end latency (non-streamed)",
        quantiles("relay_request_duration_seconds", '{stream="false"}'),
        "s",
        12,
        4,
    ),
    series(
        "Gateway overhead (excludes provider time)",
        quantiles("relay_gateway_overhead_seconds"),
        "s",
        0,
        12,
    ),
    series(
        "Time to first token (streamed)",
        quantiles("relay_time_to_first_token_seconds"),
        "s",
        12,
        12,
    ),
    series(
        "Circuit breaker state (0 closed, 1 half-open, 2 open)",
        [
            target(
                "max by (provider, instance) (relay_circuit_breaker_state)",
                "{{provider}} @ {{instance}}",
            )
        ],
        "none",
        0,
        20,
    ),
    series(
        "Upstream attempts by outcome",
        [
            target(
                "sum by (provider, outcome) (rate(relay_upstream_attempts_total[1m]))",
                "{{provider}} {{outcome}}",
            )
        ],
        "reqps",
        12,
        20,
        stack=True,
    ),
    series(
        "Cache lookups",
        [
            target(
                "sum by (layer, result) (rate(relay_cache_lookups_total[1m]))",
                "{{layer}} {{result}}",
            )
        ],
        "reqps",
        0,
        28,
        stack=True,
    ),
    series(
        "Retries and fallbacks",
        [
            target("sum by (provider) (rate(relay_retries_total[1m]))", "retry {{provider}}"),
            target(
                "sum by (provider) (rate(relay_fallbacks_total[1m]))", "fallback to {{provider}}"
            ),
        ],
        "reqps",
        12,
        28,
    ),
    series(
        "Rate-limited requests by reason",
        [target("sum by (reason) (rate(relay_rate_limited_total[1m]))", "{{reason}}")],
        "reqps",
        0,
        36,
        stack=True,
    ),
    series(
        "Tokens / s by model",
        [target("sum by (model, kind) (rate(relay_tokens_total[1m]))", "{{model}} {{kind}}")],
        "short",
        12,
        36,
        stack=True,
    ),
    series(
        "Spend and cache savings (USD / hour)",
        [
            target("sum by (model) (rate(relay_cost_usd_total[5m])) * 3600", "spend {{model}}"),
            target("sum(rate(relay_cache_savings_usd_total[5m])) * 3600", "saved by cache"),
        ],
        "currencyUSD",
        0,
        44,
    ),
    series(
        "Metering queue",
        [
            target("sum(relay_usage_queue_depth)", "queued events"),
            target("sum(rate(relay_usage_events_dropped_total[1m]))", "dropped / s"),
        ],
        "short",
        12,
        44,
    ),
]

for index, panel in enumerate(panels, start=1):
    panel["id"] = index

dashboard = {
    "uid": "relay-gateway",
    "title": "Relay gateway",
    "tags": ["relay", "llm-gateway"],
    "timezone": "utc",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "5s",
    "time": {"from": "now-15m", "to": "now"},
    "panels": panels,
    "templating": {"list": []},
    "annotations": {"list": []},
}

OUT.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {OUT} ({len(panels)} panels)")
