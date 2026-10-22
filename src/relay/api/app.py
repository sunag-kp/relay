from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from relay import __version__
from relay.api.middleware import RequestContextMiddleware
from relay.api.routes import health
from relay.bootstrap import Container, build_container
from relay.config import Settings, get_settings
from relay.observability.log import configure_logging, get_logger

log = get_logger("relay.app")

DESCRIPTION = """
One OpenAI-compatible endpoint in front of many LLM providers.

* **`POST /v1/chat/completions`**: drop-in for the OpenAI Chat Completions API (incl. streaming).
  Authenticate with `Authorization: Bearer <relay key>`.
* **`/admin/*`**: key management and usage, authenticated with the admin token.
"""


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    """Build the ASGI app. Tests pass a pre-built `container` wired with fakes."""
    settings = settings or (container.settings if container else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or await build_container(settings)
        log.info("gateway_started", replica=settings.replica_id, env=settings.app_env)
        try:
            yield
        finally:
            if owned:
                await app.state.container.aclose()
            log.info("gateway_stopped", replica=settings.replica_id)

    app = FastAPI(
        title="Relay",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.add_middleware(RequestContextMiddleware, replica_id=settings.replica_id)
    return app


def create_app_from_env() -> FastAPI:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        json=settings.log_json,
        service=settings.service_name,
        replica=settings.replica_id,
    )
    return create_app(settings)
