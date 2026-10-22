"""OpenTelemetry tracing: one trace per request, gateway -> Redis/Postgres -> provider.

FastAPI, httpx, Redis and asyncpg are auto-instrumented; the resilience layer adds one span per
upstream attempt (provider, model, attempt number, breaker probe, failure kind), so a slow
or failed request shows exactly which provider attempts it made and where the time went.
The W3C `traceparent` header is propagated to providers by the httpx instrumentation.
"""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from relay import __version__
from relay.config import Settings

_EXCLUDED = "healthz,readyz,metrics,dashboard"


def configure_tracing(settings: Settings) -> TracerProvider | None:
    if not settings.otel_enabled:
        return None
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": __version__,
                "service.instance.id": settings.replica_id,
                "deployment.environment": settings.app_env,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_ratio)),
    )
    exporter = OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    RedisInstrumentor().instrument(tracer_provider=provider)
    AsyncPGInstrumentor().instrument(tracer_provider=provider)  # type: ignore[no-untyped-call]
    return provider


def instrument_app(app: FastAPI, provider: TracerProvider | None) -> None:
    if provider is not None:
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls=_EXCLUDED)
