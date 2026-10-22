"""Prove that rate limits hold across horizontally scaled replicas.

Through nginx (which spreads requests over every gateway replica), fire a concurrent burst
at a fresh key and check that the whole cluster admits exactly the key's limit, not the
limit per replica, and that the admitted requests were served by several replicas.

    uv run --no-project --with httpx python scripts/verify_limits.py --base-url http://localhost:8080
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter

import httpx


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--admin-token", default="dev-admin-token")
    parser.add_argument("--rpm", type=int, default=30)
    parser.add_argument("--burst", type=int, default=150)
    args = parser.parse_args()

    limits = httpx.Limits(max_connections=args.burst)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30, limits=limits) as client:
        created = await client.post(
            "/admin/keys",
            json={"name": "limit-check", "owner": "ci", "rpm_limit": args.rpm},
            headers={"authorization": f"Bearer {args.admin_token}"},
        )
        created.raise_for_status()
        auth = {"authorization": f"Bearer {created.json()['api_key']}"}

        async def call(n: int) -> httpx.Response:
            body = {"model": "relay-mock", "messages": [{"role": "user", "content": f"q{n}"}]}
            return await client.post("/v1/chat/completions", json=body, headers=auth)

        started = time.perf_counter()
        responses = await asyncio.gather(*(call(n) for n in range(args.burst)))
        elapsed = time.perf_counter() - started

    statuses = Counter(r.status_code for r in responses)
    ok = [r for r in responses if r.status_code == 200]
    served_by = Counter(r.headers["x-relay-replica"] for r in ok)
    all_replicas = Counter(r.headers.get("x-relay-replica", "?") for r in responses)
    refill = int(elapsed * args.rpm / 60)  # the bucket keeps refilling during the burst
    print(f"burst of {args.burst} in {elapsed:.2f}s against a {args.rpm}/min key: {dict(statuses)}")
    print(f"admitted per replica: {dict(served_by)}; all responses per replica: {dict(all_replicas)}")

    failures = []
    if not args.rpm <= len(ok) <= args.rpm + refill:
        failures.append(f"admitted {len(ok)}, expected {args.rpm}..{args.rpm + refill}")
    if set(statuses) - {200, 429}:
        failures.append(f"unexpected statuses {dict(statuses)}")
    if len(all_replicas) < 2:
        failures.append("the burst did not reach more than one replica")
    if any(int(r.headers["retry-after"]) < 1 for r in responses if r.status_code == 429):
        failures.append("a 429 without a usable Retry-After")
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if not failures:
        print(f"OK: {len(ok)} admitted cluster-wide (limit {args.rpm}, refill allowance {refill})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
