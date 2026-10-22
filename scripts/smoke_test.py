"""End-to-end smoke test against a running stack (`make up`, or CI's compose job).

Checks that nginx spreads traffic over more than one healthy gateway replica.
"""

from __future__ import annotations

import argparse
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--expect-replicas", type=int, default=2)
    args = parser.parse_args()

    replicas: set[str] = set()
    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        for _ in range(30):
            response = client.get("/readyz")
            response.raise_for_status()
            replicas.add(response.headers["x-relay-replica"])
    print(f"ready; {len(replicas)} replicas answered: {sorted(replicas)}")
    if len(replicas) < args.expect_replicas:
        print(f"expected at least {args.expect_replicas} replicas", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
