"""Errors the gateway reports to clients, in the OpenAI error vocabulary, plus the
classification of upstream provider failures that drives retries and fallback."""

from __future__ import annotations

from enum import StrEnum


class RelayError(Exception):
    """An error with a client-facing status code, OpenAI error type and code."""

    status_code = 500
    error_type = "api_error"
    code = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retry_after_s: float | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.retry_after_s = retry_after_s
        self.param = param
        self.headers = headers or {}


class InvalidRequest(RelayError):
    status_code = 400
    error_type = "invalid_request_error"
    code = "invalid_request"


class AuthenticationFailed(RelayError):
    status_code = 401
    error_type = "authentication_error"
    code = "invalid_api_key"


class PermissionDenied(RelayError):
    status_code = 403
    error_type = "permission_error"
    code = "forbidden"


class NotFound(RelayError):
    status_code = 404
    error_type = "invalid_request_error"
    code = "not_found"


class ModelNotFound(NotFound):
    code = "model_not_found"


class Conflict(RelayError):
    status_code = 409
    error_type = "invalid_request_error"
    code = "conflict"


class RateLimited(RelayError):
    status_code = 429
    error_type = "rate_limit_error"
    code = "rate_limit_exceeded"


class ServiceUnavailable(RelayError):
    status_code = 503
    error_type = "api_error"
    code = "service_unavailable"


class NoProviderAvailable(RelayError):
    """Every target for the route is short-circuited (open breakers); fail fast."""

    status_code = 503
    error_type = "api_error"
    code = "no_provider_available"


class GatewayTimeout(RelayError):
    status_code = 504
    error_type = "api_error"
    code = "deadline_exceeded"


class AllProvidersFailed(RelayError):
    status_code = 502
    error_type = "api_error"
    code = "upstream_error"


class UpstreamRejected(RelayError):
    """The provider rejected the request itself (400/422): another provider would too."""

    status_code = 400
    error_type = "invalid_request_error"
    code = "upstream_rejected"


class DependencyUnavailable(Exception):
    """A backing store (Redis, Postgres) did not answer. Adapters raise this instead of
    leaking driver exceptions; services decide whether to fail open or closed."""


class FailureKind(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMITED = "rate_limited"  # the provider throttled us
    SERVER = "server"  # 5xx / overloaded
    AUTH = "auth"  # our credentials for this provider are wrong
    BAD_REQUEST = "bad_request"  # the request is invalid for this provider
    PROTOCOL = "protocol"  # malformed upstream response or stream


# Statuses worth retrying on the same provider (transient by definition).
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
# Statuses meaning the request itself is wrong: surface to the client, no fallback.
CLIENT_ERROR_STATUSES = frozenset({400, 413, 422})


class ProviderFailure(Exception):
    """One failed attempt against one provider."""

    def __init__(
        self,
        provider: str,
        kind: FailureKind,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"{provider}: {kind}: {message}")
        self.provider = provider
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after_s = retry_after_s

    @property
    def retryable(self) -> bool:
        """Worth another attempt on the same provider after a backoff."""
        return self.kind in {
            FailureKind.TIMEOUT,
            FailureKind.CONNECTION,
            FailureKind.RATE_LIMITED,
            FailureKind.SERVER,
        }

    @property
    def is_client_error(self) -> bool:
        """The request is at fault, not the provider: do not retry, fall back or trip."""
        return self.kind is FailureKind.BAD_REQUEST

    @classmethod
    def from_status(
        cls,
        provider: str,
        status: int,
        message: str,
        *,
        retry_after_s: float | None = None,
    ) -> ProviderFailure:
        if status == 429:
            kind = FailureKind.RATE_LIMITED
        elif status in CLIENT_ERROR_STATUSES:
            kind = FailureKind.BAD_REQUEST
        elif status in {401, 403, 404}:
            kind = FailureKind.AUTH
        elif status in RETRYABLE_STATUSES or status >= 500:
            kind = FailureKind.SERVER
        else:
            kind = FailureKind.PROTOCOL
        return cls(provider, kind, message, status=status, retry_after_s=retry_after_s)
