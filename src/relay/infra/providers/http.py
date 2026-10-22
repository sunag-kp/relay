"""Shared HTTP plumbing for provider adapters: pooled httpx clients, error mapping and a
small Server-Sent Events reader."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from relay.domain.catalog import ProviderSpec
from relay.domain.errors import FailureKind, ProviderFailure


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str | None
    data: str


def build_client(spec: ProviderSpec, headers: dict[str, str]) -> httpx.AsyncClient:
    """One pooled client per provider, so connections (and TLS sessions) are reused."""
    return httpx.AsyncClient(
        base_url=spec.base_url.rstrip("/"),
        headers={**headers, **spec.headers},
        timeout=httpx.Timeout(spec.timeout_s, connect=spec.connect_timeout_s),
        limits=httpx.Limits(
            max_connections=spec.max_connections,
            max_keepalive_connections=spec.max_connections,
            keepalive_expiry=30,
        ),
    )


def api_key_for(spec: ProviderSpec) -> str | None:
    return os.environ.get(spec.api_key_env) if spec.api_key_env else None


def stream_timeout(spec: ProviderSpec) -> httpx.Timeout:
    # Between chunks the read timeout acts as an idle timeout; the time to the first chunk
    # is bounded separately by the resilience layer.
    return httpx.Timeout(spec.stream_idle_timeout_s, connect=spec.connect_timeout_s)


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


def failure_from_response(provider: str, response: httpx.Response, body: bytes) -> ProviderFailure:
    return ProviderFailure.from_status(
        provider,
        response.status_code,
        error_message(body),
        retry_after_s=parse_retry_after(response.headers.get("retry-after")),
    )


def failure_from_exception(provider: str, exc: Exception) -> ProviderFailure:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderFailure(provider, FailureKind.TIMEOUT, type(exc).__name__)
    if isinstance(exc, httpx.TransportError):
        return ProviderFailure(provider, FailureKind.CONNECTION, f"{type(exc).__name__}: {exc}")
    return ProviderFailure(provider, FailureKind.PROTOCOL, f"{type(exc).__name__}: {exc}")


async def post_json(
    client: httpx.AsyncClient, provider: str, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = await client.post(path, json=body)
    except httpx.HTTPError as exc:
        raise failure_from_exception(provider, exc) from exc
    if response.status_code >= 400:
        raise failure_from_response(provider, response, response.content)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderFailure(provider, FailureKind.PROTOCOL, "response is not JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderFailure(provider, FailureKind.PROTOCOL, "response is not a JSON object")
    return payload


@asynccontextmanager
async def open_sse(
    client: httpx.AsyncClient,
    provider: str,
    path: str,
    body: dict[str, Any],
    read_timeout: httpx.Timeout,
) -> AsyncIterator[AsyncIterator[SSEEvent]]:
    """POST and yield an iterator over the response's SSE events.

    HTTP errors are raised before the first event, so callers can still fall back to
    another provider; errors after that surface mid-iteration.
    """
    request = client.build_request("POST", path, json=body, timeout=read_timeout)
    try:
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise failure_from_exception(provider, exc) from exc
    try:
        if response.status_code >= 400:
            raw = await response.aread()
            raise failure_from_response(provider, response, raw)
        yield _iter_sse(response, provider)
    finally:
        await response.aclose()


async def _iter_sse(response: httpx.Response, provider: str) -> AsyncIterator[SSEEvent]:
    event: str | None = None
    data: list[str] = []
    try:
        async for line in response.aiter_lines():
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
    except httpx.HTTPError as exc:
        raise failure_from_exception(provider, exc) from exc


def loads_event(provider: str, data: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ProviderFailure(provider, FailureKind.PROTOCOL, "malformed stream event") from exc
    if not isinstance(payload, dict):
        raise ProviderFailure(provider, FailureKind.PROTOCOL, "stream event is not an object")
    return payload
