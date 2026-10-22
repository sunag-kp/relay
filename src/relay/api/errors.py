"""Every error leaves the gateway in the OpenAI error envelope, so OpenAI SDKs raise their
usual typed exceptions (RateLimitError, AuthenticationError, ...) against Relay too."""

from __future__ import annotations

import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from relay.domain.errors import RelayError
from relay.observability.log import get_logger

log = get_logger("relay.errors")


def error_body(
    message: str, error_type: str, code: str, param: str | None = None
) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "code": code, "param": param}}


def relay_error_response(exc: RelayError) -> JSONResponse:
    headers = dict(exc.headers)
    if exc.retry_after_s is not None:
        headers["retry-after"] = str(max(1, math.ceil(exc.retry_after_s)))
    return JSONResponse(
        error_body(exc.message, exc.error_type, exc.code, exc.param),
        status_code=exc.status_code,
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RelayError)
    async def _relay_error(_request: Request, exc: RelayError) -> JSONResponse:
        if exc.status_code >= 500:
            log.warning("request_failed", status=exc.status_code, code=exc.code, error=exc.message)
        return relay_error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = (
            f"{location}: {first.get('msg', 'invalid request')}"
            if location
            else "Invalid request body."
        )
        return JSONResponse(
            error_body(message, "invalid_request_error", "invalid_request", location or None),
            status_code=400,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error_type = "invalid_request_error" if exc.status_code < 500 else "api_error"
        return JSONResponse(
            error_body(str(exc.detail), error_type, f"http_{exc.status_code}"),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )
