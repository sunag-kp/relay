"""Run ASGI apps on real sockets inside the test's event loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn


@asynccontextmanager
async def serve(app: Any, *, lifespan: str = "auto") -> AsyncIterator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan=lifespan)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
