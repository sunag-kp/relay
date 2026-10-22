from __future__ import annotations

import json
import logging

import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider

from relay.observability.log import configure_logging, get_logger


def test_logs_are_json_with_static_fields_and_trace_ids(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True, service="relay-test", replica="r1")
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("unit"):
        structlog.contextvars.bind_contextvars(request_id="req-1")
        get_logger("relay.test").info("hello", answer=42)
        structlog.contextvars.clear_contextvars()
    logging.getLogger("uvicorn.error").warning("from stdlib")

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    ours = next(line for line in lines if line["event"] == "hello")
    assert ours["answer"] == 42
    assert ours["request_id"] == "req-1"
    assert ours["service"] == "relay-test"
    assert ours["replica"] == "r1"
    assert len(ours["trace_id"]) == 32
    assert len(ours["span_id"]) == 16
    stdlib = next(line for line in lines if line["event"] == "from stdlib")
    assert stdlib["level"] == "warning"
    assert stdlib["service"] == "relay-test"
