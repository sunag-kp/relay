"""Usage metering off the hot path, and the reports built from it."""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from relay.domain.keys import ApiKey
from relay.domain.usage import UsageEvent, roll_up
from relay.observability import metrics
from relay.observability.log import get_logger
from relay.services.ports import DailyUsageRow, UsageStore

log = get_logger("relay.usage")


class UsageRecorder:
    """Buffers usage events in memory and writes them in batches.

    `record` never blocks or does I/O, so metering adds nothing to request latency. The
    cost is durability: events still in the buffer are lost if the process is killed
    (graceful shutdown flushes them). When the store is slower than the traffic, the bounded
    buffer drops events (counted in a metric) rather than growing without limit.
    """

    def __init__(
        self,
        store: UsageStore,
        *,
        batch_size: int = 500,
        flush_interval_s: float = 1.0,
        max_queue: int = 50_000,
    ) -> None:
        self._store = store
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._queue: asyncio.Queue[UsageEvent] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def record(self, event: UsageEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            metrics.USAGE_DROPPED.inc()
            return
        metrics.USAGE_QUEUE_DEPTH.set(self._queue.qsize())

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="usage-writer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()

    async def flush(self) -> int:
        """Write everything buffered right now. Returns the number of events written."""
        written = 0
        while not self._queue.empty():
            written += await self._write_batch()
        return written

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval_s)
            try:
                await self.flush()
            except Exception as exc:
                log.warning("usage_flush_failed", error=repr(exc))

    async def _write_batch(self) -> int:
        async with self._lock:
            batch: list[UsageEvent] = []
            while len(batch) < self._batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            metrics.USAGE_QUEUE_DEPTH.set(self._queue.qsize())
            if not batch:
                return 0
            try:
                await self._store.write(batch, roll_up(batch))
            except Exception:
                # Put the batch back (as far as it fits) so a short outage loses nothing.
                for event in batch:
                    self.record(event)
                raise
            return len(batch)


# ---------------------------------------------------------------- reports


@dataclass(frozen=True, slots=True)
class Totals:
    requests: int
    errors: int
    cache_hits: int
    semantic_hits: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    saved_usd: Decimal
    avg_latency_ms: float

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.requests if self.requests else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "cache_hits": self.cache_hits,
            "semantic_hits": self.semantic_hits,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cost_usd": float(self.cost_usd),
            "saved_usd": float(self.saved_usd),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
        }


def _totals(rows: Iterable[DailyUsageRow]) -> Totals:
    rows = list(rows)
    requests = sum(r.requests for r in rows)
    return Totals(
        requests=requests,
        errors=sum(r.errors for r in rows),
        cache_hits=sum(r.cache_hits for r in rows),
        semantic_hits=sum(r.semantic_hits for r in rows),
        prompt_tokens=sum(r.prompt_tokens for r in rows),
        completion_tokens=sum(r.completion_tokens for r in rows),
        cost_usd=sum((r.cost_usd for r in rows), Decimal(0)),
        saved_usd=sum((r.saved_usd for r in rows), Decimal(0)),
        avg_latency_ms=sum(r.latency_ms_sum for r in rows) / requests if requests else 0.0,
    )


def _grouped(
    rows: Iterable[DailyUsageRow], key: Callable[[DailyUsageRow], Any]
) -> dict[Any, Totals]:
    groups: dict[Any, list[DailyUsageRow]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return {group: _totals(items) for group, items in groups.items()}


class UsageReports:
    def __init__(self, store: UsageStore, *, today: Callable[[], date] | None = None) -> None:
        self._store = store
        self._today = today or (lambda: datetime.now(UTC).date())

    async def summary(
        self, *, days: int, keys: list[ApiKey], api_key_id: UUID | None = None
    ) -> dict[str, Any]:
        until = self._today()
        since = until - timedelta(days=days - 1)
        rows = await self._store.daily(since, until, api_key_id)
        names = {key.id: key for key in keys}

        by_day = _grouped(rows, lambda r: r.day)
        series = []
        for offset in range(days):
            day = since + timedelta(days=offset)
            totals = by_day.get(day)
            series.append(
                {"day": day.isoformat(), **(totals.as_dict() if totals else _totals([]).as_dict())}
            )

        by_key = []
        for key_id, totals in sorted(
            _grouped(rows, lambda r: r.api_key_id).items(), key=lambda kv: -kv[1].requests
        ):
            key = names.get(key_id)
            by_key.append(
                {
                    "api_key_id": str(key_id),
                    "name": key.name if key else "(deleted)",
                    "owner": key.owner if key else "",
                    **totals.as_dict(),
                }
            )

        return {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "totals": _totals(rows).as_dict(),
            "by_day": series,
            "by_key": by_key,
            "by_route": _named(_grouped(rows, lambda r: r.route), "route"),
            "by_provider": _named(_grouped(rows, lambda r: r.provider), "provider"),
        }

    async def recent(self, limit: int, api_key_id: UUID | None = None) -> list[UsageEvent]:
        return await self._store.recent(limit, api_key_id)


def _named(groups: dict[Any, Totals], label: str) -> list[dict[str, Any]]:
    return [
        {label: name, **totals.as_dict()}
        for name, totals in sorted(groups.items(), key=lambda kv: -kv[1].requests)
    ]
