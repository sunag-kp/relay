"""Load test: throughput and latency at 1 vs 3 gateway replicas, and the gateway's overhead.

For each replica count the stack is rescaled, then k6 offers each arrival rate for the
configured duration. The same rates are also sent straight to the mock provider: the
difference between the two is what the gateway adds. Server-side, the gateway's own
overhead histogram (request time minus time spent waiting on the provider) is read from
Prometheus for the same window.

    make load-test
    # = uv run --no-project --with httpx python benchmarks/run_load_test.py, which takes
    #   --replicas 1 3 --rates 100 200 300 400 600 --modes bypass cold hit --duration 30s

Needs the compose stack (`make up`) and Docker (k6 runs in the grafana/k6 image).
Results land in benchmarks/results/<date>/: results.json (every number), README.md (the
tables) and raw/ (k6's own summaries).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
K6_IMAGE = "grafana/k6:1.4.2"
PROM = "http://localhost:9090"


def sh(*args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        args, check=True, capture_output=True, text=True, env={**os.environ, **(env or {})}
    )
    return result.stdout


def machine() -> dict[str, Any]:
    cpu = ""
    try:
        cpu = next(
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        )
    except (OSError, StopIteration):
        cpu = platform.processor()
    mem_kb = 0
    with contextlib.suppress(OSError, StopIteration):
        meminfo = Path("/proc/meminfo").read_text().splitlines()
        mem_kb = int(next(line for line in meminfo if line.startswith("MemTotal")).split()[1])
    return {
        "cpu": cpu,
        "vcpus": os.cpu_count(),
        "memory_gb": round(mem_kb / 1024 / 1024, 1),
        "os": platform.platform(),
    }


def scale(replicas: int, env: dict[str, str]) -> None:
    sh(
        "docker",
        "compose",
        "up",
        "-d",
        "--wait",
        "--scale",
        f"gateway={replicas}",
        "gateway",
        "nginx",
        env=env,
    )
    with httpx.Client(base_url="http://localhost:8080", timeout=5) as client:
        seen: set[str] = set()
        deadline = time.time() + 60
        while len(seen) < replicas and time.time() < deadline:
            response = client.get("/readyz")
            if response.status_code == 200:
                seen.add(response.headers["x-relay-replica"])
    if len(seen) < replicas:
        raise RuntimeError(f"only {len(seen)} of {replicas} replicas answered through nginx")


def create_key(admin_token: str) -> str:
    response = httpx.post(
        "http://localhost:8080/admin/keys",
        json={"name": "load-test", "owner": "bench", "rpm_limit": 1_000_000, "tpd_limit": 10**12},
        headers={"authorization": f"Bearer {admin_token}"},
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["api_key"])


def configure_mock(latency_ms: float) -> None:
    # Fixed latency, no jitter, no failures: the provider is a constant, the gateway the variable.
    httpx.put(
        "http://localhost:9000/_control/profiles/alpha",
        json={"latency_ms": latency_ms, "jitter_ms": 0, "failure_rate": 0},
        timeout=5,
    ).raise_for_status()


def cooldown(seconds: float) -> None:
    # An overloaded run leaves a backlog in the gateways; let it drain before the next one.
    time.sleep(seconds)


def k6(out_dir: Path, name: str, env: dict[str, str]) -> dict[str, Any]:
    args = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{ROOT / 'benchmarks/k6'}:/scripts:ro",
        "-v",
        f"{out_dir}:/out",
    ]
    for key, value in {**env, "OUT": f"/out/{name}.json"}.items():
        args += ["-e", f"{key}={value}"]
    args += ["--name", f"relay-k6-{name}", K6_IMAGE, "run", "--quiet", "/scripts/chat.js"]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.6 * _seconds(env.get("DURATION", "30s")))
    cpu = container_cpu()
    process.wait()
    summary = json.loads((out_dir / f"{name}.json").read_text())
    metrics = summary["metrics"]
    duration = metrics["http_req_duration"]["values"]
    return {
        "achieved_rps": round(metrics["http_reqs"]["values"]["rate"], 1),
        "requests": int(metrics["http_reqs"]["values"]["count"]),
        "p50_ms": round(duration["med"], 2),
        "p95_ms": round(duration["p(95)"], 2),
        "p99_ms": round(duration["p(99)"], 2),
        "max_ms": round(duration["max"], 2),
        "error_rate": round(metrics["http_req_failed"]["values"]["rate"], 4),
        "dropped_iterations": int(
            metrics.get("dropped_iterations", {}).get("values", {}).get("count", 0)
        ),
        "cpu_percent": cpu,
    }


def _seconds(duration: str) -> float:
    units = {"s": 1, "m": 60}
    return float(duration[:-1]) * units[duration[-1]]


def container_cpu() -> dict[str, float]:
    """CPU use per service mid-step (100 = one vCPU), summed over a service's replicas."""
    out = sh("docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}")
    usage: dict[str, float] = {}
    for line in out.splitlines():
        name, _, value = line.partition(" ")
        if name.startswith("relay-k6-"):
            service = "k6"
        elif name.startswith("relay-"):
            service = name.removeprefix("relay-").rsplit("-", 1)[0]
        else:
            continue
        usage[service] = round(usage.get(service, 0.0) + float(value.rstrip("%")), 1)
    return usage


def overhead_quantiles(window_s: int, at: float) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for q in (0.5, 0.95, 0.99):
        query = (
            f"histogram_quantile({q}, sum by (le) "
            f'(increase(relay_gateway_overhead_seconds_bucket{{stream="false"}}[{window_s}s])))'
        )
        response = httpx.get(
            f"{PROM}/api/v1/query", params={"query": query, "time": at}, timeout=10
        )
        result = response.json()["data"]["result"]
        out[f"p{int(q * 100)}_ms"] = (
            round(float(result[0]["value"][1]) * 1000, 3) if result else None
        )
    return out


MODES = {
    "bypass": "cache bypassed (`Cache-Control: no-cache, no-store`)",
    "cold": "cache on, new prompts (exact miss, semantic search, then both caches written)",
    "hit": "cache on, one repeated prompt (exact hits)",
}


def sustained(run: dict[str, Any]) -> bool:
    """The step kept up: at least 97% of the offered requests completed (requests k6 had to
    drop count against it), with under 1% errors."""
    return bool(run["achieved_rps"] >= 0.97 * run["rate"] and run["error_rate"] < 0.01)


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


def report(results: dict[str, Any]) -> str:
    """The results as markdown tables; every cell comes from results.json."""
    m, settings = results["machine"], results["settings"]
    runs = results["runs"]
    mock = {r["rate"]: r for r in runs if r["target"] == "mock"}
    gateway = [r for r in runs if r["target"] == "gateway"]
    lines = [
        f"# Load test, {results['started_at'][:10]}",
        "",
        f"Machine: {m['cpu']}, {m['vcpus']} vCPUs, {m['memory_gb']} GB RAM ({m['os']}). "
        "Everything ran on this one machine and shared its CPUs: k6, nginx, the gateway "
        "replicas (one Python process each), the mock provider, Redis and Postgres.",
        "",
        f"Each step offers a constant arrival rate for {settings['duration']} (k6 "
        "`constant-arrival-rate`, so a saturated gateway shows up as latency and dropped "
        "iterations, not as a quietly lower load). The mock provider answers in a fixed "
        f"{settings['mock_latency_ms']:g} ms. Every gateway request takes the full path: "
        "API-key auth, rate-limit admission in Redis, routing, the provider call (unless "
        "cached), quota settlement and usage metering. Tracing samples 1% of requests. "
        "Cache modes:",
        "",
        *[f"- **{mode}**: {text}" for mode, text in MODES.items()],
        "",
        "```",
        results["command"],
        "```",
        "",
        "## Highest sustained rate",
        "",
        "The highest offered rate a configuration kept up with (at least 97% of the "
        "offered requests completed, under 1% errors), and the most it completed at any "
        "step.",
        "",
        "| Replicas | Cache | Sustained req/s | p50 / p95 ms there | Peak achieved req/s |",
        "|---|---|---|---|---|",
    ]
    for replicas in settings["replicas"]:
        for mode in MODES:
            steps = [r for r in gateway if r["replicas"] == replicas and r["cache"] == mode]
            if not steps:
                continue
            ok = [r for r in steps if sustained(r)]
            best = max(ok, key=lambda r: r["rate"]) if ok else None
            peak = max(r["achieved_rps"] for r in steps)
            rate, at = (
                ("none", "-")
                if best is None
                else (
                    best["rate"],
                    f"{best['p50_ms']:g} / {best['p95_ms']:g}",
                )
            )
            lines.append(f"| {replicas} | {mode} | {rate} | {at} | {peak:g} |")
    lines += [
        "",
        "## Every step",
        "",
        "| Target | Replicas | Cache | Offered req/s | Achieved req/s | p50 ms | p95 ms "
        "| p99 ms | Errors | Dropped |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        target = "mock provider (direct)" if r["target"] == "mock" else "gateway via nginx"
        lines.append(
            f"| {target} | {r['replicas'] or '-'} | {r['cache']} | {r['rate']} "
            f"| {r['achieved_rps']:g} | {r['p50_ms']:g} | {r['p95_ms']:g} | {r['p99_ms']:g} "
            f"| {r['error_rate']:.2%} | {r['dropped_iterations']} |"
        )
    services = ["gateway", "postgres", "redis", "mock-provider", "nginx", "k6"]
    lines += [
        "",
        "## Where the CPU went",
        "",
        f"`docker stats` sampled once, 60% into each step (100% = one of the "
        f"{m['vcpus']} vCPUs; gateway is the sum over its replicas; total also counts "
        "Prometheus, Grafana and Jaeger).",
        "",
        "| Replicas | Cache | Offered req/s | " + " | ".join(services) + " | total |",
        "|---|---|---|" + "---|" * (len(services) + 1),
    ]
    for r in gateway:
        cpu = r.get("cpu_percent", {})
        cells = [f"{cpu.get(service, 0):g}" for service in services]
        lines.append(
            f"| {r['replicas']} | {r['cache']} | {r['rate']} | "
            + " | ".join(cells)
            + f" | {sum(cpu.values()):.0f} |"
        )
    lines += [
        "",
        "## Gateway overhead",
        "",
        "Only steps the gateway sustained. *Added p50/p95* is the gateway's p50/p95 minus "
        "the mock provider's at the same offered rate, as k6 saw it (it includes the extra "
        "nginx hop). *Server p50/p95/p99* is `relay_gateway_overhead_seconds` from "
        "Prometheus: time inside the gateway's request handler not spent waiting on the "
        "provider (it excludes time queued before the handler runs).",
        "",
        "| Replicas | Cache | Offered req/s | Added p50 ms | Added p95 ms | Server p50 ms "
        "| Server p95 ms | Server p99 ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in gateway:
        if r["cache"] == "hit" or r["rate"] not in mock or not sustained(r):
            continue
        base, over = mock[r["rate"]], r["server_overhead"]
        lines.append(
            f"| {r['replicas']} | {r['cache']} | {r['rate']} "
            f"| {r['p50_ms'] - base['p50_ms']:.2f} | {r['p95_ms'] - base['p95_ms']:.2f} "
            f"| {_ms(over['p50_ms'])} | {_ms(over['p95_ms'])} | {_ms(over['p99_ms'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def flush_cache(admin_token: str) -> None:
    httpx.post(
        "http://localhost:8080/admin/cache/flush",
        headers={"authorization": f"Bearer {admin_token}"},
        timeout=60,
    ).raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replicas", type=int, nargs="+", default=[1, 3])
    parser.add_argument("--rates", type=int, nargs="+", default=[100, 200, 300, 400, 600])
    parser.add_argument("--modes", nargs="+", choices=list(MODES), default=list(MODES))
    parser.add_argument("--duration", default="30s")
    parser.add_argument("--mock-latency-ms", type=float, default=50.0)
    parser.add_argument("--admin-token", default="dev-admin-token")
    parser.add_argument("--cooldown", type=float, default=15.0, help="seconds between runs")
    parser.add_argument("--name", default=dt.date.today().isoformat())
    args = parser.parse_args()

    out_dir = ROOT / "benchmarks" / "results" / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "raw"
    raw.mkdir(exist_ok=True)
    # Production-like tracing: sample 1% of requests.
    env = {"OTEL_TRACES_SAMPLER_RATIO": "0.01"}
    results: dict[str, Any] = {
        "machine": machine(),
        "settings": {**vars(args), "otel_sampling": 0.01},
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "runs": [],
    }

    configure_mock(args.mock_latency_ms)
    for rate in args.rates:
        print(f"baseline: mock provider directly at {rate}/s", flush=True)
        stats = k6(
            raw, f"mock-{rate}", {"TARGET": "mock", "RATE": str(rate), "DURATION": args.duration}
        )
        results["runs"].append(
            {"target": "mock", "replicas": 0, "rate": rate, "cache": "n/a", **stats}
        )

    for replicas in args.replicas:
        print(f"scaling gateway to {replicas} replica(s)", flush=True)
        scale(replicas, env)
        configure_mock(args.mock_latency_ms)
        flush_cache(args.admin_token)  # every replica count starts from the same empty cache
        key = create_key(args.admin_token)
        gateway = {"TARGET": "gateway", "API_KEY": key, "DURATION": args.duration}
        k6(raw, f"warmup-{replicas}", {**gateway, "RATE": "50", "DURATION": "10s"})

        for mode in args.modes:
            for rate in args.rates:
                print(f"gateway x{replicas}, {mode}, at {rate}/s", flush=True)
                started = time.time()
                stats = k6(
                    raw,
                    f"gw{replicas}-{mode}-{rate}",
                    {**gateway, "RATE": str(rate), "CACHE": mode},
                )
                time.sleep(6)  # let Prometheus scrape the tail of the window
                overhead = overhead_quantiles(int(time.time() - started), time.time())
                results["runs"].append(
                    {"target": "gateway", "replicas": replicas, "rate": rate, "cache": mode}
                    | stats
                    | {"server_overhead": overhead}
                )
                cooldown(args.cooldown)

    results["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
    results["command"] = "uv run --no-project --with httpx python benchmarks/run_load_test.py " + (
        " ".join(sys.argv[1:])
    )
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (out_dir / "README.md").write_text(report(results))
    print(f"wrote {out_dir / 'results.json'} and {out_dir / 'README.md'}")


if __name__ == "__main__":
    main()
