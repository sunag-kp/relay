# Diagnostics behind the load test

Measurements taken while investigating the load-test results, on the same machine as the
load test (4 vCPUs shared by everything). The load test in this folder ran on commit
`ba67070`. Commands are as run; outputs are copied from the terminal.

## 1. Upstream HTTP client: httpx → aiohttp

One gateway process outside Docker (`uv run relay-gateway` on port 8100, against the
compose Postgres, Redis and mock provider), k6 offering 200 req/s with the cache bypassed,
sampled for 15 s:

```
uvx py-spy record --pid <gateway pid> --duration 15 --format raw -o profile.txt
```

Inclusive share of samples, before (httpx 0.28):

| Frame | Share |
|---|---|
| httpx / httpcore (whole upstream call) | 64% |
| httpcore connection pool, `is_socket_readable` on every idle connection | 28% |

After switching to aiohttp 3.14 (commit `4dd847d`), same load: the upstream call
(`post_json`, aiohttp `__aenter__`) is 16–18% of samples. Probe runs through nginx, one
replica, cache bypassed, mock provider at 50 ms:

| Client | Offered | Achieved | p50 |
|---|---|---|---|
| httpx | 100 req/s | 90 req/s | 262 ms |
| aiohttp | 200 req/s | 198.4 req/s | 57 ms |

## 2. Semantic-cache lookup: generic plan vs HNSW

After a cold-prompt run left 6,130 rows in one namespace (9,614 rows in total), the same
query with the namespace as a literal (a custom plan):

```sql
EXPLAIN ANALYZE SELECT response, embedding <=> :'v'::vector AS d FROM semantic_cache
WHERE namespace = :'ns' AND expires_at > now() ORDER BY d LIMIT 1;
```

```
Limit (actual time=4.295..4.297 rows=1 loops=1)
  ->  Index Scan using ix_semantic_cache_embedding_hnsw on semantic_cache
Execution Time: 4.332 ms
```

and as the generic plan chose it for a parameterised namespace (estimated as a handful of
rows, so the btree index and a sort):

```
->  Bitmap Heap Scan on semantic_cache (actual time=10.124..65.292 rows=6130 loops=1)
      ->  Bitmap Index Scan on ix_semantic_cache_namespace
Execution Time: 68.395 ms
```

Fix: migration `0004` (`plan_cache_mode = force_custom_plan` and the HNSW settings as
database defaults).

## 3. Where Postgres time goes on the cold path

`pg_stat_statements` enabled on the local Postgres only, the semantic cache truncated, one
gateway replica, k6 offering 80 req/s of new prompts (`CACHE=cold`) for 25 s:

```sql
SELECT calls, round(total_exec_time::numeric,0) AS total_ms, round(mean_exec_time::numeric,2) AS mean_ms,
       left(query, 60) FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 4;
```

```
 calls | total_ms | mean_ms | query
-------+----------+---------+------------------------------------------------------------
  1976 |    13623 |    6.89 | SELECT semantic_cache.response, semantic_cache.embedding <=>
  1976 |     7967 |    4.03 | INSERT INTO semantic_cache (namespace, embedding, response,
  1957 |       61 |    0.03 | INSERT INTO usage_events (request_id, created_at, api_key_id
   181 |       15 |    0.08 | INSERT INTO usage_daily (day, api_key_id, route, provider, r
```

About 11 ms of Postgres time per new prompt, nearly all of it the semantic cache; usage
metering is negligible. Right after a truncate the planner's statistics describe an empty
table until autoanalyze runs (it ran every minute here), so the search brute-forces the
namespace for that first minute. In an idle database with fresh statistics the same search
took 1.7–1.9 ms through HNSW at 18,251 rows, and an insert about 2 ms (100 rows in one
statement: 198 ms).

Autovacuum was ruled out as the cause (`log_autovacuum_min_duration = 0`): each automatic
vacuum and analyze of `semantic_cache` used at most 0.33 s of CPU, once a minute.

## 4. Rejected: an UNLOGGED semantic-cache table

Sampling `pg_stat_activity` during the cold-path load showed the inserts mostly waiting on
the write-ahead log (`LWLock:WALWrite`, `WALInsert`, `IO:WalSync`) and on index pages
(`LWLock:BufferContent`). An unlogged table skips the WAL. Measured in isolation with
pgbench, 8 clients inserting single rows into a 384-dimension HNSW-indexed table for 15 s:

```
pgbench -n -U relay -d relay -c 8 -j 4 -T 15 -f insert.sql
```

| Table | Inserts/s | Mean latency |
|---|---|---|
| logged | 715 | 11.2 ms |
| unlogged | 807 | 9.9 ms |

13% more insert throughput. Not adopted: unlogged tables are emptied after a crash and, on
Neon, whenever the compute restarts, which on the free tier is after every idle period.
