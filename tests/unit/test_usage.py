from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from relay.domain.keys import ApiKey
from relay.domain.usage import UsageEvent, roll_up
from relay.services.usage import UsageRecorder, UsageReports
from tests.fakes import FakeUsageStore

KEY_A, KEY_B = uuid.uuid4(), uuid.uuid4()


def event(key: uuid.UUID = KEY_A, **overrides: object) -> UsageEvent:
    values: dict[str, object] = {
        "request_id": uuid.uuid4().hex,
        "api_key_id": key,
        "owner": "team",
        "route": "relay-mock",
        "provider": "alpha",
        "provider_model": "a1",
        "status": "ok",
        "cache": "miss",
        "stream": False,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost_usd": Decimal("0.001"),
        "latency_ms": 100,
        "attempts": 1,
    }
    values.update(overrides)
    return UsageEvent(**values)  # type: ignore[arg-type]


def test_rollup_groups_by_day_key_route_and_provider() -> None:
    yesterday = datetime.now(UTC) - timedelta(days=1)
    rollups = roll_up(
        [
            event(),
            event(cache="exact", provider="alpha", cost_usd=Decimal(0), saved_usd=Decimal("0.001")),
            event(
                status="upstream_error", prompt_tokens=0, completion_tokens=0, cost_usd=Decimal(0)
            ),
            event(key=KEY_B),
            event(created_at=yesterday),
            event(provider=None, status="no_provider_available"),
        ]
    )
    by_key = {(r.day, r.api_key_id, r.provider): r for r in rollups}
    today = by_key[(datetime.now(UTC).date(), KEY_A, "alpha")]
    assert (today.requests, today.errors, today.cache_hits) == (3, 1, 1)
    assert today.prompt_tokens == 10 + 10 + 0
    assert today.cost_usd == Decimal("0.001")  # the hit and the error cost nothing
    assert today.saved_usd == Decimal("0.001")
    assert len(rollups) == 4
    assert (datetime.now(UTC).date(), KEY_A, "-") in by_key


async def test_recorder_batches_and_flushes() -> None:
    store = FakeUsageStore()
    recorder = UsageRecorder(store, batch_size=3)
    for _ in range(7):
        recorder.record(event())
    assert store.events == []  # recording never does I/O
    assert await recorder.flush() == 7
    assert store.writes == 3  # 3 + 3 + 1
    assert sum(row.requests for row in store.daily_rows.values()) == 7


async def test_recorder_drops_when_full_instead_of_growing() -> None:
    store = FakeUsageStore()
    recorder = UsageRecorder(store, max_queue=2)
    for _ in range(5):
        recorder.record(event())
    assert await recorder.flush() == 2


async def test_failed_write_keeps_the_events_for_the_next_flush() -> None:
    store = FakeUsageStore()
    store.fail_next = 1
    recorder = UsageRecorder(store)
    recorder.record(event())
    with pytest.raises(ConnectionError):
        await recorder.flush()
    assert await recorder.flush() == 1
    assert len(store.events) == 1


async def test_recorder_flushes_on_stop() -> None:
    store = FakeUsageStore()
    recorder = UsageRecorder(store, flush_interval_s=3600)
    recorder.start()
    recorder.record(event())
    await recorder.stop()
    assert len(store.events) == 1


def api_key(key_id: uuid.UUID, name: str) -> ApiKey:
    return ApiKey(key_id, name, "team", "rk_x", 10, 100, None, False, None, datetime.now(UTC))


async def test_summary_totals_series_and_breakdowns() -> None:
    store = FakeUsageStore()
    today = date(2026, 10, 22)
    at = datetime(2026, 10, 22, 12, tzinfo=UTC)
    events = [
        event(created_at=at),
        event(
            created_at=at,
            cache="semantic",
            cost_usd=Decimal(0),
            saved_usd=Decimal("0.001"),
            prompt_tokens=0,
            completion_tokens=0,
        ),
        event(key=KEY_B, created_at=at - timedelta(days=2), route="gpt-5-mini", provider="openai"),
        event(created_at=at - timedelta(days=30)),  # outside the window
    ]
    await store.write(events, roll_up(events))
    reports = UsageReports(store, today=lambda: today)

    summary = await reports.summary(days=7, keys=[api_key(KEY_A, "search"), api_key(KEY_B, "chat")])

    totals = summary["totals"]
    assert totals["requests"] == 3
    assert totals["cache_hits"] == 1
    assert totals["semantic_hits"] == 1
    assert totals["cache_hit_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert totals["cost_usd"] == pytest.approx(0.002)
    assert totals["saved_usd"] == pytest.approx(0.001)
    assert len(summary["by_day"]) == 7
    assert summary["by_day"][-1]["requests"] == 2
    assert summary["by_day"][0]["requests"] == 0
    assert [k["name"] for k in summary["by_key"]] == ["search", "chat"]
    assert {r["route"] for r in summary["by_route"]} == {"relay-mock", "gpt-5-mini"}
    only_b = await reports.summary(days=7, keys=[], api_key_id=KEY_B)
    assert only_b["totals"]["requests"] == 1
    assert only_b["by_key"][0]["name"] == "(deleted)"


async def test_periodic_jobs_run_at_start_and_survive_errors() -> None:
    import asyncio

    from relay.services.background import PeriodicTasks

    runs: list[str] = []

    async def ok() -> None:
        runs.append("ok")

    async def broken() -> None:
        runs.append("broken")
        raise RuntimeError("boom")

    periodic = PeriodicTasks()
    periodic.every(0.01, ok, name="ok")
    periodic.every(0.01, broken, name="broken")
    periodic.start()
    await asyncio.sleep(0.035)
    await periodic.stop()
    assert runs.count("ok") >= 2
    assert runs.count("broken") >= 2
