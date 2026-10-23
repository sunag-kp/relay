"""Shared HTTP plumbing for provider adapters: pooled aiohttp sessions, error mapping and a
small Server-Sent Events reader.

aiohttp rather than httpx on this path: profiling the gateway under load showed 64% of its
CPU inside httpx/httpcore, mostly the connection pool checking every idle socket for
readability on each request. aiohttp's connector does not, and upstream calls are the
gateway's hottest code.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from relay.domain.catalog import ProviderSpec
from relay.domain.errors import FailureKind, ProviderFailure


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str | None
    data: str


def api_key_for(spec: ProviderSpec) -> str | None:
    return os.environ.get(spec.api_key_env) if spec.api_key_env else None


class UpstreamSession:
    """One pooled HTTP session per provider (connections and TLS sessions are reused).

    Created lazily inside the running event loop, as aiohttp requires."""

    def __init__(self, spec: ProviderSpec, headers: Mapping[str, str]) -> None:
        self.spec = spec
        self.base_url = spec.base_url.rstrip("/")
        self.headers = {**headers, **spec.headers}
        self._session: aiohttp.ClientSession | None = None

    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=self.spec.max_connections,
                limit_per_host=self.spec.max_connections,
                keepalive_timeout=30,
                ttl_dns_cache=300,
            )
            self._session = aiohttp.ClientSession(
                headers=self.headers, connector=connector, raise_for_status=False
            )
        return self._session

    def request_timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self.spec.timeout_s, connect=self.spec.connect_timeout_s)

    def stream_timeout(self) -> aiohttp.ClientTimeout:
        # Between chunks the socket-read timeout acts as an idle timeout; the time to the
        # first chunk is bounded separately by the resilience layer.
        return aiohttp.ClientTimeout(
            total=None,
            connect=self.spec.connect_timeout_s,
            sock_read=self.spec.stream_idle_timeout_s,
        )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


def error_message(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except ValueError:
        return body[:300].decode(errors="replace") or "no body"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
    return str(payload)[:300]


def failure_from_status(
    provider: str, status: int, headers: Mapping[str, str], body: bytes
) -> ProviderFailure:
    return ProviderFailure.from_status(
        provider,
        status,
        error_message(body),
        retry_after_s=parse_retry_after(headers.get("retry-after")),
    )


def failure_from_exception(provider: str, exc: BaseException) -> ProviderFailure:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, aiohttp.ServerTimeoutError)):
        return ProviderFailure(provider, FailureKind.TIMEOUT, type(exc).__name__)
    if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, OSError)):
        return ProviderFailure(provider, FailureKind.CONNECTION, f"{type(exc).__name__}: {exc}")
    return ProviderFailure(provider, FailureKind.PROTOCOL, f"{type(exc).__name__}: {exc}")


async def post_json(upstream: UpstreamSession, path: str, body: dict[str, Any]) -> dict[str, Any]:
    provider = upstream.spec.name
    try:
        async with upstream.session().post(
            upstream.base_url + path, json=body, timeout=upstream.request_timeout()
        ) as response:
            raw = await response.read()
            status, headers = response.status, response.headers
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise failure_from_exception(provider, exc) from exc
    if status >= 400:
        raise failure_from_status(provider, status, headers, raw)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ProviderFailure(provider, FailureKind.PROTOCOL, "response is not JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderFailure(provider, FailureKind.PROTOCOL, "response is not a JSON object")
    return payload


@asynccontextmanager
async def open_sse(
    upstream: UpstreamSession, path: str, body: dict[str, Any]
) -> AsyncIterator[AsyncIterator[SSEEvent]]:
    """POST and yield an iterator over the response's SSE events.

    HTTP errors are raised before the first event, so callers can still fall back to
    another provider; errors after that surface mid-iteration.
    """
    provider = upstream.spec.name
    try:
        response = await upstream.session().post(
            upstream.base_url + path, json=body, timeout=upstream.stream_timeout()
        )
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise failure_from_exception(provider, exc) from exc
    try:
        if response.status >= 400:
            raw = await response.read()
            raise failure_from_status(provider, response.status, response.headers, raw)
        yield _iter_sse(response, provider)
    finally:
        # Closing (rather than releasing) drops a connection whose stream was abandoned
        # half-way, so it is never reused with unread bytes on the wire.
        if response.content.at_eof():
            response.release()
        else:
            response.close()


async def _iter_sse(response: aiohttp.ClientResponse, provider: str) -> AsyncIterator[SSEEvent]:
    event: str | None = None
    data: list[str] = []
    try:
        async for raw_line in response.content:
            line = raw_line.decode().rstrip("\r\n")
            if not line:
                if data:
                    yield SSEEvent(event, "\n".join(data))
                event, data = None, []
                continue
            if line.startswith(":"):
                continue  # comment / keep-alive
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)
        if data:
            yield SSEEvent(event, "\n".join(data))
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise failure_from_exception(provider, exc) from exc


def loads_event(provider: str, data: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ProviderFailure(provider, FailureKind.PROTOCOL, "malformed stream event") from exc
    if not isinstance(payload, dict):
        raise ProviderFailure(provider, FailureKind.PROTOCOL, "stream event is not an object")
    return payload
