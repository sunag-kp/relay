"""Fire-and-forget work that must not be lost when the request that started it ends.

Settling quotas after a stream, writing cache entries and recording usage happen after the
response is (partly) sent, possibly while the client disconnects. Tasks are tracked so they
are not garbage-collected mid-flight and so shutdown can drain them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from relay.observability.log import get_logger

log = get_logger("relay.background")


class BackgroundTasks:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background_task_failed", task=task.get_name(), error=repr(task.exception()))

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def drain(self, timeout_s: float = 10.0) -> None:
        if not self._tasks:
            return
        _, still_running = await asyncio.wait(set(self._tasks), timeout=timeout_s)
        for task in still_running:
            task.cancel()


class PeriodicTasks:
    """Housekeeping loops (cache purge, health publishing) started and stopped with the app.

    Every replica runs them; the jobs are idempotent, so no leader election is needed."""

    def __init__(self) -> None:
        self._jobs: list[tuple[str, float, Callable[[], Awaitable[Any]]]] = []
        self._tasks: list[asyncio.Task[None]] = []

    def every(self, interval_s: float, job: Callable[[], Awaitable[Any]], *, name: str) -> None:
        self._jobs.append((name, interval_s, job))

    def start(self) -> None:
        for name, interval_s, job in self._jobs:
            self._tasks.append(asyncio.create_task(self._loop(name, interval_s, job), name=name))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    @staticmethod
    async def _loop(name: str, interval_s: float, job: Callable[[], Awaitable[Any]]) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await job()
            except Exception as exc:
                log.warning("periodic_job_failed", job=name, error=repr(exc))
