"""Admission control: per-key request rate, daily token quota and monthly budget."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from relay.domain.chat import ChatCompletionRequest
from relay.domain.errors import (
    DependencyUnavailable,
    InvalidRequest,
    RateLimited,
    ServiceUnavailable,
)
from relay.domain.keys import ApiKey
from relay.domain.limits import (
    Admission,
    LimitCheck,
    LimitReason,
    budget_period,
    from_micro_usd,
    seconds_to_period_end,
    to_micro_usd,
)
from relay.domain.tokens import estimate_reservation
from relay.observability import metrics
from relay.observability.log import get_logger
from relay.services.ports import RateLimiter

log = get_logger("relay.limits")


@dataclass(frozen=True, slots=True)
class Reservation:
    """An admitted request's claim on its key's limits, settled once usage is known."""

    key: ApiKey
    check: LimitCheck | None  # None when the limiter was down and we failed open
    admission: Admission | None

    def headers(self) -> dict[str, str]:
        if self.admission is None:
            return {}
        return {
            "x-ratelimit-limit-requests": str(self.key.rpm_limit),
            "x-ratelimit-remaining-requests": str(self.admission.remaining_requests),
            "x-ratelimit-limit-tokens": str(self.key.tpd_limit),
            "x-ratelimit-remaining-tokens": str(self.admission.remaining_tokens),
        }


@dataclass(frozen=True, slots=True)
class SpendUpdate:
    key: ApiKey
    period: str
    before_micro_usd: int
    after_micro_usd: int


class QuotaService:
    def __init__(
        self,
        limiter: RateLimiter,
        *,
        default_completion_tokens: int = 256,
        fail_open: bool = True,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._limiter = limiter
        self._default_completion_tokens = default_completion_tokens
        self._fail_open = fail_open
        self._now = now

    async def admit(self, key: ApiKey, request: ChatCompletionRequest) -> Reservation:
        tokens = estimate_reservation(request, self._default_completion_tokens)
        if tokens > key.tpd_limit:
            raise InvalidRequest(
                f"This request needs about {tokens} tokens, more than this key's daily quota "
                f"of {key.tpd_limit}. Lower max_tokens or shorten the prompt.",
                code="request_too_large",
            )
        now = self._now()
        enforced_budget = (
            to_micro_usd(key.monthly_budget_usd)
            if key.enforce_budget and key.monthly_budget_usd is not None
            else None
        )
        check = LimitCheck(
            key_id=str(key.id),
            rpm_limit=key.rpm_limit,
            tpd_limit=key.tpd_limit,
            tokens=tokens,
            budget_micro_usd=enforced_budget,
            period=budget_period(now),
            seconds_to_period_end=seconds_to_period_end(now),
        )
        try:
            admission = await self._limiter.admit(check)
        except DependencyUnavailable as exc:
            if not self._fail_open:
                raise ServiceUnavailable(
                    "Rate limiting is unavailable; try again shortly."
                ) from exc
            # Availability over strictness: an outage of the limiter must not take the whole
            # gateway down. Provider-side limits and budgets still apply.
            log.warning("rate_limiter_unavailable_failing_open", error=str(exc))
            metrics.RATE_LIMITER_ERRORS.inc()
            return Reservation(key=key, check=None, admission=None)

        reservation = Reservation(key=key, check=check, admission=admission)
        if not admission.allowed:
            assert admission.reason is not None
            metrics.RATE_LIMITED.labels(reason=admission.reason.value).inc()
            raise _denied(key, admission, reservation.headers())
        return reservation

    async def settle(
        self, reservation: Reservation, *, tokens: int, cost_usd: Decimal
    ) -> SpendUpdate | None:
        """Replace the token estimate with real usage and add the spend. Refunds the whole
        reservation when the request failed (tokens=0)."""
        if reservation.check is None:
            return None
        spend = to_micro_usd(cost_usd)
        try:
            _, total = await self._limiter.settle(reservation.check, tokens, spend)
        except DependencyUnavailable as exc:
            log.warning("quota_settle_failed", key_id=reservation.check.key_id, error=str(exc))
            return None
        return SpendUpdate(
            key=reservation.key,
            period=reservation.check.period,
            before_micro_usd=total - spend,
            after_micro_usd=total,
        )


def _denied(key: ApiKey, admission: Admission, headers: dict[str, str]) -> RateLimited:
    retry_after = max(admission.retry_after_s, 0.001)
    if admission.reason is LimitReason.REQUESTS:
        return RateLimited(
            f"Rate limit reached: {key.rpm_limit} requests per minute for this API key.",
            code="rate_limit_exceeded",
            retry_after_s=retry_after,
            headers=headers,
        )
    if admission.reason is LimitReason.TOKENS:
        return RateLimited(
            f"Token quota reached: {key.tpd_limit} tokens per day for this API key.",
            code="token_quota_exceeded",
            retry_after_s=retry_after,
            headers=headers,
        )
    budget = from_micro_usd(to_micro_usd(key.monthly_budget_usd or Decimal(0)))
    return RateLimited(
        f"This API key has used its monthly budget of ${budget:.2f}.",
        code="insufficient_quota",
        retry_after_s=retry_after,
        headers=headers,
    )
