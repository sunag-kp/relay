"""Pure-ASGI middleware: request ids, access log, replica header.

Written against raw ASGI rather than Starlette's BaseHTTPMiddleware so it adds no per-request
task or body buffering: streaming responses pass through untouched and the gateway's own
overhead stays small.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from relay.observability.log import get_logger

log = get_logger("relay.access")

REQUEST_ID_HEADER = b"x-request-id"
_QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, replica_id: str) -> None:
        self.app = app
        self.replica_header = (b"x-relay-replica", replica_id.encode())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER, request_id.encode()))
                headers.append(self.replica_header)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in _QUIET_PATHS:
                log.info(
                    "http_request",
                    method=scope.get("method"),
                    path=path,
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            structlog.contextvars.clear_contextvars()


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER:
            candidate: str = value.decode("latin-1")
            # Only accept sane ids from clients; anything else gets a fresh one.
            if 0 < len(candidate) <= 128 and candidate.replace("-", "").isalnum():
                return candidate
            return None
    return None
