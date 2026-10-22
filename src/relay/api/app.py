from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from relay import __version__
from relay.api.errors import install_error_handlers
from relay.api.middleware import RequestContextMiddleware
from relay.api.routes import admin, chat, health
from relay.bootstrap import Container, build_container
from relay.config import Settings, get_settings
from relay.mock_provider.app import create_mock_app
from relay.observability.log import configure_logging, get_logger
from relay.services.keys import KeySpec

log = get_logger("relay.app")

DESCRIPTION = """
One OpenAI-compatible endpoint in front of many LLM providers.

* **`POST /v1/chat/completions`**: drop-in for the OpenAI Chat Completions API (incl. streaming).
  Authenticate with `Authorization: Bearer <relay key>`.
* **`/admin/*`**: key management and usage, authenticated with the admin token.
"""


async def _provision_demo_key(container: Container) -> None:
    settings = container.settings
    if settings.demo_api_key is None:
        return
    key = await container.keys.ensure(
        settings.demo_api_key.get_secret_value(),
        KeySpec(
            name="public demo",
            owner="demo",
            rpm_limit=settings.demo_rpm_limit,
            tpd_limit=settings.demo_tpd_limit,
        ),
    )
    log.info("demo_key_ready", key_id=str(key.id), prefix=key.prefix)


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    """Build the ASGI app. Tests pass a pre-built `container` wired with fakes."""
    settings = settings or (container.settings if container else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or await build_container(settings)
        try:
            await _provision_demo_key(app.state.container)
            app.state.container.start()
            log.info("gateway_started", replica=settings.replica_id, env=settings.app_env)
            yield
        finally:
            if owned:
                await app.state.container.aclose()
            else:
                await app.state.container.stop()
            log.info("gateway_stopped", replica=settings.replica_id)

    app = FastAPI(
        title="Relay",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(chat.router)
    app.include_router(admin.router)
    app.include_router(health.router)
    if settings.mount_mock_provider:
        app.mount("/mock", create_mock_app())
    dashboard = settings.ui_dist_dir
    if (dashboard / "index.html").is_file():
        app.mount("/dashboard", StaticFiles(directory=dashboard, html=True), name="dashboard")

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        if (dashboard / "index.html").is_file():
            return RedirectResponse("/dashboard/")
        return JSONResponse({"name": "relay", "docs": "/docs", "health": "/healthz"})

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
