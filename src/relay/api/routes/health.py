from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

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


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
