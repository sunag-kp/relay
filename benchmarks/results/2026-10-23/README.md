# Load test, 2026-10-23

Machine: Intel(R) Xeon(R) Processor @ 2.80GHz, 4 vCPUs, 15.7 GB RAM (Linux-6.18.44-fc-v140-x86_64-with-glibc2.39). Everything ran on this one machine and shared its CPUs: k6, nginx, the gateway replicas (one Python process each), the mock provider, Redis and Postgres.

Each step offers a constant arrival rate for 30s (k6 `constant-arrival-rate`, so a saturated gateway shows up as latency and dropped iterations, not as a quietly lower load). The mock provider answers in a fixed 50 ms. Every gateway request takes the full path: API-key auth, rate-limit admission in Redis, routing, the provider call (unless cached), quota settlement and usage metering. Tracing samples 1% of requests. Cache modes:

- **bypass**: cache bypassed (`Cache-Control: no-cache, no-store`)
- **cold**: cache on, new prompts (exact miss, semantic search, then both caches written)
- **hit**: cache on, one repeated prompt (exact hits)

```
uv run --no-project --with httpx python benchmarks/run_load_test.py --replicas 1 3 --rates 100 200 300 400 600 --duration 30s
```

## Highest sustained rate

The highest offered rate a configuration kept up with (at least 97% of the offered requests completed, under 1% errors), and the most it completed at any step.

| Replicas | Cache | Sustained req/s | p50 / p95 ms there | Peak achieved req/s |
|---|---|---|---|---|
| 1 | bypass | 200 | 58.37 / 127.21 | 232.4 |
| 1 | cold | 100 | 80.23 / 280.39 | 106.7 |
| 1 | hit | 200 | 6.41 / 27.05 | 249.9 |
| 3 | bypass | 400 | 69.66 / 250.87 | 478.1 |
| 3 | cold | 100 | 79.8 / 351.13 | 156.2 |
| 3 | hit | 400 | 5.44 / 32.31 | 508.5 |

## Every step

| Target | Replicas | Cache | Offered req/s | Achieved req/s | p50 ms | p95 ms | p99 ms | Errors | Dropped |
|---|---|---|---|---|---|---|---|---|---|
| mock provider (direct) | - | n/a | 100 | 99.9 | 50.72 | 52.25 | 53.8 | 0.00% | 0 |
| mock provider (direct) | - | n/a | 200 | 199.7 | 50.79 | 51.98 | 53.55 | 0.00% | 0 |
| mock provider (direct) | - | n/a | 300 | 299.5 | 50.37 | 51.94 | 53.61 | 0.00% | 0 |
| mock provider (direct) | - | n/a | 400 | 399.3 | 50.77 | 52.06 | 54.43 | 0.00% | 0 |
| mock provider (direct) | - | n/a | 600 | 599 | 50.55 | 51.9 | 53.83 | 0.00% | 0 |
| gateway via nginx | 1 | bypass | 100 | 99.8 | 55.42 | 59.99 | 68.23 | 0.00% | 0 |
| gateway via nginx | 1 | bypass | 200 | 199.2 | 58.37 | 127.21 | 292.46 | 0.00% | 14 |
| gateway via nginx | 1 | bypass | 300 | 226.9 | 706.2 | 13704.2 | 21164 | 0.00% | 1585 |
| gateway via nginx | 1 | bypass | 400 | 232.4 | 612.09 | 25027 | 27415.4 | 0.00% | 4270 |
| gateway via nginx | 1 | bypass | 600 | 229.7 | 719.42 | 29120.6 | 29854.9 | 0.15% | 9964 |
| gateway via nginx | 1 | cold | 100 | 99.1 | 80.23 | 280.39 | 409 | 0.00% | 14 |
| gateway via nginx | 1 | cold | 200 | 96.4 | 480.75 | 17956.2 | 26859.7 | 0.37% | 2755 |
| gateway via nginx | 1 | cold | 300 | 104.8 | 522.48 | 25115.8 | 30001.1 | 2.55% | 5041 |
| gateway via nginx | 1 | cold | 400 | 106.7 | 565.19 | 28031.3 | 30001 | 2.63% | 7746 |
| gateway via nginx | 1 | cold | 600 | 99.9 | 656.21 | 30000.8 | 30001.6 | 13.57% | 13587 |
| gateway via nginx | 1 | hit | 100 | 100 | 4.74 | 8.06 | 14.8 | 0.00% | 0 |
| gateway via nginx | 1 | hit | 200 | 200 | 6.41 | 27.05 | 49.28 | 0.00% | 0 |
| gateway via nginx | 1 | hit | 300 | 231.4 | 494.78 | 15070.8 | 20906.4 | 0.00% | 1546 |
| gateway via nginx | 1 | hit | 400 | 242.5 | 481.29 | 24215.1 | 28092.8 | 0.07% | 3918 |
| gateway via nginx | 1 | hit | 600 | 249.9 | 491.19 | 28712.3 | 29426.8 | 0.27% | 9363 |
| gateway via nginx | 3 | bypass | 100 | 99.8 | 56.17 | 59.31 | 63.58 | 0.00% | 0 |
| gateway via nginx | 3 | bypass | 200 | 199.7 | 55.79 | 64.2 | 79.19 | 0.00% | 0 |
| gateway via nginx | 3 | bypass | 300 | 299.5 | 56.95 | 79.39 | 108.79 | 0.00% | 0 |
| gateway via nginx | 3 | bypass | 400 | 397.3 | 69.66 | 250.87 | 443.26 | 0.01% | 54 |
| gateway via nginx | 3 | bypass | 600 | 478.1 | 771.37 | 7747.72 | 15092.1 | 0.00% | 2442 |
| gateway via nginx | 3 | cold | 100 | 99.1 | 79.8 | 351.13 | 557.89 | 0.00% | 19 |
| gateway via nginx | 3 | cold | 200 | 93.1 | 4041.03 | 7561.63 | 9209.97 | 0.00% | 2876 |
| gateway via nginx | 3 | cold | 300 | 156.2 | 3422.2 | 8120.97 | 12502.8 | 0.00% | 3705 |
| gateway via nginx | 3 | cold | 400 | 143 | 4973.44 | 11586.6 | 16935.5 | 0.00% | 6883 |
| gateway via nginx | 3 | cold | 600 | 148.8 | 6935.24 | 15925.8 | 24008.3 | 0.21% | 12186 |
| gateway via nginx | 3 | hit | 100 | 100 | 4.73 | 6.81 | 10.02 | 0.00% | 0 |
| gateway via nginx | 3 | hit | 200 | 200 | 4.86 | 8.78 | 15.27 | 0.00% | 0 |
| gateway via nginx | 3 | hit | 300 | 300 | 5.1 | 13.26 | 31.69 | 0.00% | 0 |
| gateway via nginx | 3 | hit | 400 | 399.9 | 5.44 | 32.31 | 122.46 | 0.00% | 1 |
| gateway via nginx | 3 | hit | 600 | 508.5 | 213.14 | 6331.9 | 9734.28 | 0.05% | 1458 |

## Where the CPU went

`docker stats` sampled once, 60% into each step (100% = one of the 4 vCPUs; gateway is the sum over its replicas; total also counts Prometheus, Grafana and Jaeger).

| Replicas | Cache | Offered req/s | gateway | postgres | redis | mock-provider | nginx | k6 | total |
|---|---|---|---|---|---|---|---|---|---|
| 1 | bypass | 100 | 51.3 | 0.7 | 7.8 | 9 | 3.4 | 13.3 | 87 |
| 1 | bypass | 200 | 86.8 | 1 | 8.2 | 13.9 | 9.7 | 23.8 | 145 |
| 1 | bypass | 300 | 120 | 2.9 | 5.2 | 12 | 5.2 | 18.2 | 165 |
| 1 | bypass | 400 | 107.7 | 6.3 | 8.2 | 12.6 | 9.2 | 20.9 | 166 |
| 1 | bypass | 600 | 102 | 1.5 | 8.4 | 13.7 | 4.3 | 18.5 | 150 |
| 1 | cold | 100 | 121.8 | 156 | 6.8 | 8.8 | 7.1 | 16 | 318 |
| 1 | cold | 200 | 85.2 | 178.4 | 7.5 | 13.2 | 4.9 | 10.7 | 302 |
| 1 | cold | 300 | 139.8 | 75.4 | 10.6 | 45.8 | 6.1 | 13.1 | 292 |
| 1 | cold | 400 | 104.8 | 80.7 | 7.7 | 54.2 | 2.8 | 13.2 | 265 |
| 1 | cold | 600 | 101.2 | 114.3 | 10.2 | 7.2 | 2.8 | 13.6 | 251 |
| 1 | hit | 100 | 49.6 | 8.5 | 6.5 | 50.6 | 3.8 | 16.5 | 142 |
| 1 | hit | 200 | 142.5 | 8.6 | 7.1 | 0.5 | 6.1 | 16.8 | 183 |
| 1 | hit | 300 | 100.4 | 12 | 9.7 | 38.5 | 8.2 | 24.4 | 194 |
| 1 | hit | 400 | 101.2 | 9.3 | 6.9 | 0.5 | 5.3 | 17.9 | 143 |
| 1 | hit | 600 | 101.6 | 8.6 | 6.1 | 39.4 | 4.3 | 17.7 | 179 |
| 3 | bypass | 100 | 58.4 | 1.3 | 4.9 | 8.4 | 3.5 | 13.4 | 91 |
| 3 | bypass | 200 | 131.8 | 1.6 | 8.7 | 17.7 | 10.1 | 23.7 | 196 |
| 3 | bypass | 300 | 141.9 | 2.4 | 12.9 | 23.4 | 10.9 | 21.7 | 215 |
| 3 | bypass | 400 | 184.5 | 3.4 | 15.8 | 23.7 | 9.5 | 34.9 | 273 |
| 3 | bypass | 600 | 257.4 | 1.2 | 9 | 19.7 | 10.9 | 35.6 | 335 |
| 3 | cold | 100 | 160.4 | 137.3 | 10.7 | 9.6 | 3.5 | 16.2 | 339 |
| 3 | cold | 200 | 90.3 | 241 | 5.6 | 23.7 | 2.8 | 13.6 | 384 |
| 3 | cold | 300 | 155.6 | 140.8 | 9.1 | 24.8 | 7.6 | 21.1 | 361 |
| 3 | cold | 400 | 171.8 | 148.5 | 8.8 | 11.6 | 7.9 | 18.6 | 369 |
| 3 | cold | 600 | 168.9 | 154.6 | 8.1 | 10.2 | 3.8 | 15 | 362 |
| 3 | hit | 100 | 78.2 | 9.4 | 5.2 | 0.5 | 6 | 12.4 | 113 |
| 3 | hit | 200 | 139.7 | 1.9 | 10.3 | 0.4 | 6.7 | 23.1 | 185 |
| 3 | hit | 300 | 186.3 | 11.1 | 16.5 | 0.4 | 8.9 | 30.9 | 255 |
| 3 | hit | 400 | 211.9 | 6.3 | 14.2 | 0.5 | 11.4 | 34.4 | 280 |
| 3 | hit | 600 | 105.3 | 1.9 | 10.5 | 0.5 | 10.4 | 57.9 | 188 |

## Gateway overhead

Only steps the gateway sustained. *Added p50/p95* is the gateway's p50/p95 minus the mock provider's at the same offered rate, as k6 saw it (it includes the extra nginx hop). *Server p50/p95/p99* is `relay_gateway_overhead_seconds` from Prometheus: time inside the gateway's request handler not spent waiting on the provider (it excludes time queued before the handler runs).

| Replicas | Cache | Offered req/s | Added p50 ms | Added p95 ms | Server p50 ms | Server p95 ms | Server p99 ms |
|---|---|---|---|---|---|---|---|
| 1 | bypass | 100 | 4.70 | 7.74 | 0.816 | 2.553 | 4.499 |
| 1 | bypass | 200 | 7.58 | 75.23 | 1.552 | 14.374 | 37.244 |
| 1 | cold | 100 | 29.51 | 228.14 | 21.973 | 206.471 | 318.947 |
| 3 | bypass | 100 | 5.45 | 7.06 | 0.824 | 1.893 | 3.089 |
| 3 | bypass | 200 | 5.00 | 12.22 | 0.863 | 3.054 | 7.04 |
| 3 | bypass | 300 | 6.58 | 27.45 | 1.098 | 6.653 | 13.731 |
| 3 | bypass | 400 | 18.89 | 198.81 | 4.061 | 41.271 | 80.877 |
| 3 | cold | 100 | 29.08 | 298.88 | 22.513 | 327.468 | 529.423 |
