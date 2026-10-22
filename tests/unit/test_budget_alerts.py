from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from relay.domain.keys import ApiKey
from relay.services.alerts import BudgetAlerts
from relay.services.limits import SpendUpdate
from tests.fakes import FakeAlertStore, FakeNotifier


def key(budget: str | None) -> ApiKey:
    return ApiKey(
        uuid.uuid4(),
        "search",
        "team",
        "rk_x",
        10,
        100,
        Decimal(budget) if budget else None,
        False,
        None,
        datetime.now(UTC),
    )


async def test_each_threshold_fires_once_per_period() -> None:
    store, notifier = FakeAlertStore(), FakeNotifier()
    alerts = BudgetAlerts(store, notifier)
    k = key("10.00")  # 10 USD = 10_000_000 micro-USD

    assert await alerts.check(SpendUpdate(k, "2026-10", 0, 4_000_000)) == []
    fired = await alerts.check(SpendUpdate(k, "2026-10", 4_000_000, 8_500_000))
    assert [a.threshold_pct for a in fired] == [50, 80]
    # another replica observing a crossing that already alerted fires nothing new
    assert await alerts.check(SpendUpdate(k, "2026-10", 4_900_000, 5_100_000)) == []
    final = await alerts.check(SpendUpdate(k, "2026-10", 8_500_000, 10_000_000))
    assert [a.threshold_pct for a in final] == [100]
    assert final[0].spend_usd == Decimal(10)
    assert [a.threshold_pct for a in notifier.sent] == [50, 80, 100]
    # a new month starts over
    assert [
        a.threshold_pct for a in await alerts.check(SpendUpdate(k, "2026-11", 0, 5_000_000))
    ] == [50]
    assert len(await alerts.recent()) == 4


async def test_keys_without_budget_never_alert() -> None:
    alerts = BudgetAlerts(FakeAlertStore())
    assert await alerts.check(SpendUpdate(key(None), "2026-10", 0, 10**9)) == []
