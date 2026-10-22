"""Per-key limits: requests per minute, tokens per day and a monthly spend budget."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

MICRO = Decimal(1_000_000)


class LimitReason(StrEnum):
    REQUESTS = "requests"
    TOKENS = "tokens"
    BUDGET = "budget"


@dataclass(frozen=True, slots=True)
class LimitCheck:
    """What one admission asks for."""

    key_id: str
    rpm_limit: int
    tpd_limit: int
    tokens: int
    budget_micro_usd: int | None  # None: no enforced budget
    period: str  # budget period, e.g. "2026-10"
    seconds_to_period_end: int


@dataclass(frozen=True, slots=True)
class Admission:
    allowed: bool
    reason: LimitReason | None
    retry_after_s: float
    remaining_requests: int
    remaining_tokens: int


def to_micro_usd(amount: Decimal) -> int:
    """Spend is counted in integer micro-dollars (rounded up, so budgets never under-count)."""
    return math.ceil(amount * MICRO)


def from_micro_usd(micro: int) -> Decimal:
    return Decimal(micro) / MICRO


def budget_period(now: datetime) -> str:
    return now.strftime("%Y-%m")


def seconds_to_period_end(now: datetime) -> int:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return max(1, math.ceil((datetime(year, month, 1, tzinfo=UTC) - now).total_seconds()))


def crossed_thresholds(
    before_micro: int, after_micro: int, budget_micro: int, thresholds: tuple[int, ...]
) -> list[int]:
    """Budget alert thresholds (percent) crossed by moving spend from before to after."""
    if budget_micro <= 0:
        return []
    return [
        pct for pct in thresholds if before_micro * 100 < pct * budget_micro <= after_micro * 100
    ]
