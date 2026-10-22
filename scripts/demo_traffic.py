"""Generate a realistic mix of traffic against a running stack, for demos and screenshots.

Creates three keys (different teams and limits), then sends requests across the routes:
repeated prompts (exact cache hits), near-duplicates (semantic hits), streams, a burst that
trips one key's rate limit, and a window where one mock provider fails (fallback + breaker).

    uv run --no-project --with httpx python scripts/demo_traffic.py --base-url http://localhost:8080
"""

from __future__ import annotations

import argparse
import asyncio
import random

import httpx

QUESTIONS = [
    "What is the capital of France?",
    "Explain the CAP theorem in two sentences.",
    "How do I reverse a list in Python?",
    "What does HTTP status 429 mean?",
    "Summarize the plot of Hamlet.",
    "Write a haiku about autumn leaves.",
    "List three benefits of unit testing.",
    "What is a circuit breaker in distributed systems?",
]
NEAR_DUPLICATES = [
    "what is the capital of france",
    "Explain the CAP theorem in two sentences!",
    "how do i reverse a list in python?",
]
ROUTES = ["relay-mock", "relay-mock-weighted", "relay-mock-cheapest", "relay-mock-fastest"]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--admin-token", default="dev-admin-token")
    parser.add_argument("--mock-url", default="http://localhost:9000")
    parser.add_argument("--requests", type=int, default=120)
    args = parser.parse_args()
    rng = random.Random(7)
    admin = {"authorization": f"Bearer {args.admin_token}"}

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        keys = []
        for name, owner, rpm, budget in [
            ("search-backend", "team-search", 600, "5.00"),
            # ~$0.00004 per mock request: this traffic crosses the 50% budget alert.
            ("support-bot", "team-support", 600, "0.01"),
            ("batch-jobs", "team-data", 20, None),
        ]:
            body: dict[str, object] = {"name": name, "owner": owner, "rpm_limit": rpm}
            if budget:
                body["monthly_budget_usd"] = budget
            response = await client.post("/admin/keys", json=body, headers=admin)
            response.raise_for_status()
            keys.append(response.json()["api_key"])

        async def ask(key: str, question: str, *, route: str, stream: bool = False) -> int:
            payload = {
                "model": route,
                "messages": [{"role": "user", "content": question}],
                "stream": stream,
            }
            response = await client.post(
                "/v1/chat/completions", json=payload, headers={"authorization": f"Bearer {key}"}
            )
            await response.aread()
            return response.status_code

        statuses: list[int] = []
        for i in range(args.requests):
            key = keys[i % 2]
            question = rng.choice(QUESTIONS + NEAR_DUPLICATES)
            statuses.append(await ask(key, question, route=rng.choice(ROUTES), stream=i % 5 == 0))

        # A burst over batch-jobs' 20 requests/minute.
        statuses += await asyncio.gather(
            *(ask(keys[2], f"Classify ticket {n}", route="relay-mock") for n in range(30))
        )

        # Break mock-alpha for a while: requests fall back to beta and breakers open.
        async with httpx.AsyncClient(base_url=args.mock_url) as mock:
            await mock.put("/_control/profiles/alpha", json={"failure_rate": 1.0})
            try:
                for n in range(40):
                    statuses.append(await ask(keys[1], f"Unique question {n}", route="relay-mock"))
            finally:
                await mock.put("/_control/profiles/alpha", json={"failure_rate": 0.0})

    summary = {code: statuses.count(code) for code in sorted(set(statuses))}
    print(f"sent {len(statuses)} requests: {summary}")


if __name__ == "__main__":
    asyncio.run(main())
