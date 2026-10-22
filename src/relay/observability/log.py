"""Structured JSON logging (structlog), with the active trace and request ids on every line."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from opentelemetry import trace


def _add_trace_context(
    _logger: Any, _method: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging(level: str = "INFO", *, json: bool = True, **static: str) -> None:
    """Route both structlog and stdlib logging (uvicorn, httpx, ...) through one JSON renderer."""
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_context,
    ]
    if static:
        shared.append(_static_fields(static))
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    # The gateway writes its own access log with request ids and timings.
    for name in ("uvicorn.access",):
        logging.getLogger(name).disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _static_fields(fields: dict[str, str]) -> structlog.typing.Processor:
    def processor(
        _logger: Any, _method: str, event_dict: structlog.typing.EventDict
    ) -> structlog.typing.EventDict:
        for key, value in fields.items():
            event_dict.setdefault(key, value)
        return event_dict

    return processor


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
