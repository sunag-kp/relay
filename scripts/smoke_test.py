"""End-to-end smoke test against a running stack (`make up`, or CI's compose job).

Through nginx: every replica is ready, traffic spreads over several replicas, and a chat
completion works both as JSON and as a token stream.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--api-key", default="rk_local_dev_key")
    parser.add_argument("--model", default="relay-mock")
    parser.add_argument("--expect-replicas", type=int, default=2)
    args = parser.parse_args()
    auth = {"authorization": f"Bearer {args.api_key}"}
    body = {"model": args.model, "messages": [{"role": "user", "content": "smoke test"}]}

    with httpx.Client(base_url=args.base_url, timeout=15) as client:
        replicas: set[str] = set()
        for _ in range(30):
            response = client.get("/readyz")
            response.raise_for_status()
            replicas.add(response.headers["x-relay-replica"])
        print(f"ready; {len(replicas)} replicas answered: {sorted(replicas)}")
        if len(replicas) < args.expect_replicas:
            print(f"expected at least {args.expect_replicas} replicas", file=sys.stderr)
            return 1

        response = client.post("/v1/chat/completions", json=body, headers=auth)
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"]
        print(f"completion via {response.headers['x-relay-provider']}: {answer[:60]!r}")

        chunks = 0
        with client.stream(
            "POST", "/v1/chat/completions", json={**body, "stream": True}, headers=auth
        ) as stream:
            stream.raise_for_status()
            for line in stream.iter_lines():
                if line.startswith("data: {"):
                    json.loads(line.removeprefix("data: "))
                    chunks += 1
        print(f"stream delivered {chunks} chunks")
        if chunks < 3:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
