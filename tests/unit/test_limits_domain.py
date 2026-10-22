from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from relay.domain.limits import (
    budget_period,
    crossed_thresholds,
    from_micro_usd,
    seconds_to_period_end,
    to_micro_usd,
)


def test_micro_usd_rounds_up() -> None:
    assert to_micro_usd(Decimal("0.0000011")) == 2
    assert to_micro_usd(Decimal("1.5")) == 1_500_000
    assert from_micro_usd(1_500_000) == Decimal("1.5")


def test_budget_period_and_reset() -> None:
    now = datetime(2026, 12, 31, 23, 59, 30, tzinfo=UTC)
    assert budget_period(now) == "2026-12"
    assert seconds_to_period_end(now) == 30
    assert seconds_to_period_end(datetime(2026, 10, 1, tzinfo=UTC)) == 31 * 86400


def test_thresholds_crossed_once() -> None:
    budget = 100
    assert crossed_thresholds(0, 49, budget, (50, 80, 100)) == []
    assert crossed_thresholds(49, 50, budget, (50, 80, 100)) == [50]
    assert crossed_thresholds(50, 85, budget, (50, 80, 100)) == [80]
    assert crossed_thresholds(10, 120, budget, (50, 80, 100)) == [50, 80, 100]
    assert crossed_thresholds(120, 130, budget, (50, 80, 100)) == []
    assert crossed_thresholds(0, 10, 0, (50,)) == []
