from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from relay import __version__
from relay.api.deps import get_container

router = APIRouter(tags=["health"])

READINESS_TIMEOUT_S = 2.0


class HealthResponse(BaseModel):
    status: str
    replica: str


class ReadinessResponse(BaseModel):
    status: str
    replica: str
    checks: dict[str, str]
    duration_ms: float


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
async def healthz(request: Request) -> HealthResponse:
    """The process is up and serving HTTP. Never touches dependencies, so a Redis or Postgres
    outage does not make the orchestrator kill otherwise healthy replicas."""
    return HealthResponse(status="ok", replica=get_container(request).settings.replica_id)


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse}},
)
async def readyz(request: Request, response: Response) -> ReadinessResponse:
    """Dependencies answer within the timeout, so this replica should receive traffic."""
    container = get_container(request)
    started = time.perf_counter()
    names = list(container.readiness_checks)
    results = await asyncio.gather(
        *(
            asyncio.wait_for(check(), READINESS_TIMEOUT_S)
            for check in container.readiness_checks.values()
        ),
        return_exceptions=True,
    )
    checks = {
        name: "ok" if not isinstance(result, BaseException) else f"error: {type(result).__name__}"
        for name, result in zip(names, results, strict=True)
    }
    ready = all(value == "ok" for value in checks.values())
    if not ready:
        response.status_code = 503
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        replica=container.settings.replica_id,
        checks=checks,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )


class InfoResponse(BaseModel):
    name: str
    version: str
    replica: str
    models: list[str]
    demo_api_key: str | None = None
    dashboard_public: bool


@router.get("/info", response_model=InfoResponse, summary="Public gateway info")
async def info(request: Request) -> InfoResponse:
    """What this gateway serves, and the public demo key when the deployment exposes one."""
    container = get_container(request)
    settings = container.settings
    demo_key = (
        settings.demo_api_key.get_secret_value()
        if settings.demo_api_key is not None and settings.demo_show_key
        else None
    )
    return InfoResponse(
        name="relay",
        version=__version__,
        replica=settings.replica_id,
        models=[route.model for route in container.catalog.routes],
        demo_api_key=demo_key,
        dashboard_public=settings.dashboard_public,
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
