from __future__ import annotations

import httpx
from asgi_lifespan import LifespanManager
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from relay.api.app import create_app
from relay.config import Settings
from relay.observability.tracing import configure_tracing
from tests.conftest import HarnessFactory


def test_tracing_is_off_unless_enabled() -> None:
    assert configure_tracing(Settings(otel_enabled=False)) is None


async def test_requests_produce_server_and_upstream_attempt_spans(
    make_harness: HarnessFactory,
) -> None:
    # Build a harness, then re-create its app with an in-memory tracer provider.
    harness = await make_harness()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from opentelemetry import trace

    trace.set_tracer_provider(provider)
    app = create_app(container=harness.container, tracer_provider=provider)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
                headers=harness.auth,
            )
    assert response.status_code == 200
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert "POST /v1/chat/completions" in spans
    attempt = spans["relay.upstream_attempt"]
    assert attempt.attributes is not None
    assert attempt.attributes["relay.provider"] == "alpha"
    assert attempt.attributes["relay.attempt"] == 1
    server = spans["POST /v1/chat/completions"]
    assert attempt.context.trace_id == server.context.trace_id
