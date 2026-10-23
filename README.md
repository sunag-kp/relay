# Relay

[![CI](https://github.com/sunag-kp/relay/actions/workflows/ci.yml/badge.svg)](https://github.com/sunag-kp/relay/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**A self-hostable LLM gateway. Applications call one OpenAI-compatible endpoint; Relay
handles routing, reliability, cost and limits across providers.**

Point any OpenAI SDK at Relay and get, without changing application code:

- **Routing and failover** across OpenAI, Anthropic and a mock provider: priority, weighted,
  cheapest or lowest-latency, with timeouts, retries with jitter, circuit breakers and
  fallback that also works for streaming.
- **Per-key limits that hold across replicas**: requests/minute, tokens/day and monthly
  budgets in Redis, answered with `429` and `Retry-After`.
- **Caching**: exact, plus semantic (pgvector), so near-identical prompts don't pay twice.
- **Metering**: tokens and cost per key, model and provider, daily roll-ups and budget
  alerts, in Postgres.
- **Visibility**: a dashboard, Prometheus metrics, a Grafana dashboard and OpenTelemetry
  traces of every provider attempt.

LIVE_DEMO

![Relay dashboard](docs/images/dashboard-light.png)

## Try it

```bash
curl -s LIVE_URL/v1/chat/completions \
  -H "Authorization: Bearer rk_public_demo_key_mock_routes_only" \
  -H "Content-Type: application/json" \
  -d '{"model": "relay-mock", "messages": [{"role": "user", "content": "Hello, Relay"}]}' -i
```

The response headers show what Relay did: `x-relay-provider`, `x-relay-attempts`,
`x-relay-cache` (send the same request twice), and `x-ratelimit-remaining-*`.

With the OpenAI SDK, only the base URL and key change:

```python
from openai import OpenAI

client = OpenAI(base_url="LIVE_URL/v1", api_key="rk_public_demo_key_mock_routes_only")
stream = client.chat.completions.create(
    model="relay-mock-cheapest",
    messages=[{"role": "user", "content": "Stream me something"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

## Architecture

```mermaid
flowchart LR
    app[Apps / OpenAI SDKs] --> nginx[nginx]
    nginx --> gw[Gateway replicas<br/>FastAPI, async]
    gw -- "limits, spend, exact cache,<br/>key cache (Lua, atomic)" --> redis[(Redis)]
    gw -- "keys, usage, alerts,<br/>semantic cache (pgvector)" --> pg[(Postgres)]
    gw -- "retries, breaker,<br/>fallback" --> providers[OpenAI · Anthropic · mock]
    gw -. traces .-> jaeger[Jaeger]
    prometheus[Prometheus + Grafana] -. scrape .-> gw
```

Each request goes through:

1. Authenticate the key (SHA-256 lookup, cached in Redis).
2. Admit it against the key's limits (one atomic Lua script).
3. Check the exact cache, then the semantic cache.
4. Call the providers in policy order, with retries, breakers and fallback.
5. Respond.

Quota settlement, cache writes and usage metering happen after the response, off the hot
path.

The code is layered (`api` → `services` → `domain`, with `infra` adapters behind ports),
and CI enforces the dependency rule with import-linter. The full design (requirements,
capacity estimate, data model, sequence diagrams, failure modes and trade-offs) is in
**[docs/DESIGN.md](docs/DESIGN.md)**.

## Design decisions

- **Rate limits are exact across replicas.** Each key has token buckets in Redis, and
  admission is a single Lua script that checks requests, tokens and budget, then consumes
  all or nothing. It uses Redis's clock, not each replica's. CI fires a burst of 150 requests at a
  30/minute key through nginx and 3 replicas, and checks that only the limit gets through
  (30 of 150).
- **Reserve, then settle.** Token usage is only known after the provider answers, so
  admission reserves an estimate and settlement refunds or charges the difference.
- **Circuit breakers are per replica.** Breaking needs no network hop and keeps working
  when Redis is down, at the cost of each replica learning about a failure on its own
  (Envoy's outlier detection makes the same choice).
- **Streams fall back only before the first byte.** The gateway waits for the provider's
  first event before committing the response, so most failures can still switch provider.
  After that, the client already has part of an answer.
- **Semantic cache thresholds are measured.** A labelled set of 55 prompt pairs calibrates
  each embedder: the default lexical embedder ships at 0.88, above its worst false
  positive (0.83). Prompts whose numbers differ are never compared, because "HTTP 429" and
  "HTTP 503" embed almost identically but have different answers.
- **Fail open, by default.** If Redis is down, requests are served without limits (and
  counted) instead of failing every application. One setting makes it fail closed.
- **Metering never blocks a request.** Usage is buffered in memory and written in batches.
  The bounded buffer drops (and counts) events rather than slowing requests down.

## Benchmarks

BENCH_TABLE

## Run it locally

Requirements: Docker, and [uv](https://docs.astral.sh/uv/) for the tests.

```bash
cp .env.example .env    # optional: every setting has a working default
make up                 # 3 gateway replicas behind nginx, Postgres, Redis, mock provider,
                        # Prometheus, Grafana and Jaeger
```

| What | Where |
|---|---|
| Gateway (via nginx) | http://localhost:8080, client key `rk_local_dev_key` |
| Dashboard | http://localhost:8080/dashboard/ (admin token `dev-admin-token`) |
| API docs (OpenAPI) | http://localhost:8080/docs |
| Grafana | http://localhost:3000 |
| Jaeger (traces) | http://localhost:16686 |
| Prometheus | http://localhost:9090 |

```bash
make demo-traffic   # a realistic traffic mix, to fill the dashboard and Grafana
make smoke          # end-to-end check through nginx
make verify-limits  # burst through 3 replicas: exactly the key's limit is admitted
make test           # unit + integration tests with coverage (starts Postgres and Redis)
make lint           # ruff, mypy --strict, import-linter
make load-test      # the k6 benchmark above (~30 minutes)
make down
```

Routes and providers are configured in [`config/relay.yaml`](config/relay.yaml). OpenAI and
Anthropic routes work as soon as `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` are set in `.env`.

### API

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | client key | OpenAI-compatible chat, streaming or not. `Cache-Control: no-cache` / `no-store` and `x-relay-policy` per request |
| `GET /v1/models` | client key | Routes this key may call |
| `POST/GET/PATCH/DELETE /admin/keys` | admin token | Create (key shown once), list, change limits, revoke |
| `GET /admin/usage/summary`, `/admin/usage/events` | admin* | Spend, tokens, cache savings by day, key, route and provider; recent requests |
| `GET /admin/alerts`, `/admin/providers` | admin* | Budget alerts; breaker state and latency per provider across replicas |
| `POST /admin/cache/flush` | admin token | Clear both cache layers |
| `GET /healthz`, `/readyz`, `/metrics`, `/info` | none | Liveness, readiness (Redis + Postgres), Prometheus, public info |

\* Readable without the token when `DASHBOARD_PUBLIC=true` (as on the demo).

## Deploy

The live demo runs as one container on Render's free plan, with Postgres on
[Neon](https://neon.tech) (pgvector included) and Redis on [Upstash](https://upstash.com).
[`render.yaml`](render.yaml) describes the service. The mock provider runs inside the same
process, so the demo needs no provider keys. To deploy your own:

1. Create a Neon project and copy its **pooled** connection string.
2. Create an Upstash Redis database and copy its `rediss://` URL.
3. In Render, choose **New → Blueprint**, select this repository, and paste the two values
   when asked. `ADMIN_TOKEN` is generated; it is under the service's Environment tab.

Migrations run on start. Render deploys a commit after CI passes on it.

## Project layout

```
src/relay/
  api/            FastAPI app, routes, auth, error envelope, middleware
  services/       request pipeline, quotas, cache policy, resilience, metering; ports
  domain/         pure logic: routing policies, circuit breaker, token buckets, cache keys, cost
  infra/          Redis (Lua), Postgres (SQLAlchemy Core, pgvector), provider clients, embedders
  observability/  JSON logs, Prometheus metrics, OpenTelemetry
  mock_provider/  OpenAI- and Anthropic-compatible mock with latency and failure controls
migrations/       Alembic
ui/               React + TypeScript dashboard (Vite, Recharts)
benchmarks/       k6 load test, semantic-cache threshold calibration, committed results
ops/              Prometheus, Grafana (dashboard generated from code)
deploy/           nginx
tests/            unit (fakes for every port) and integration (real Redis, Postgres, HTTP)
```

## What I'd do next

- **Lease blocks of tokens** from Redis and admit locally, to drop the per-request Redis
  round trip at very high rates (with a bounded overshoot).
- **Durable usage pipeline** (Redis Streams or Kafka) for billing-grade metering.
- **Request hedging**: after a route's p95, race a second provider and take the first answer.
- **A breaker around Redis**, so an unreachable Redis costs one timeout instead of one per call.
- **Multiple worker processes** per container to use every core of larger machines.

## License

[MIT](LICENSE)
