"""Budget alerts: notify once when a key's monthly spend crosses 50%, 80% and 100%."""

from __future__ import annotations

from relay.domain.limits import crossed_thresholds, from_micro_usd, to_micro_usd
from relay.domain.usage import BudgetAlert
from relay.observability import metrics
from relay.observability.log import get_logger
from relay.services.limits import SpendUpdate
from relay.services.ports import AlertNotifier, AlertStore

log = get_logger("relay.alerts")

THRESHOLDS = (50, 80, 100)


class BudgetAlerts:
    def __init__(self, store: AlertStore, notifier: AlertNotifier | None = None) -> None:
        self._store = store
        self._notifier = notifier

    async def check(self, update: SpendUpdate) -> list[BudgetAlert]:
        """Called with every spend increase. Spend counters live in Redis, so the
        before/after pair is exact even with many replicas adding spend concurrently."""
        budget = update.key.monthly_budget_usd
        if budget is None:
            return []
        fired = []
        for pct in crossed_thresholds(
            update.before_micro_usd, update.after_micro_usd, to_micro_usd(budget), THRESHOLDS
        ):
            alert = BudgetAlert(
                api_key_id=update.key.id,
                key_name=update.key.name,
                owner=update.key.owner,
                period=update.period,
                threshold_pct=pct,
                spend_usd=from_micro_usd(update.after_micro_usd),
                budget_usd=budget,
            )
            if not await self._store.insert_if_new(alert):
                continue
            fired.append(alert)
            metrics.BUDGET_ALERTS.labels(threshold=str(pct)).inc()
            log.warning(
                "budget_threshold_crossed",
                key_id=str(alert.api_key_id),
                key_name=alert.key_name,
                owner=alert.owner,
                threshold_pct=pct,
                spend_usd=str(alert.spend_usd),
                budget_usd=str(alert.budget_usd),
            )
            if self._notifier is not None:
                await self._notifier.notify(alert)
        return fired

    async def recent(self, limit: int = 50) -> list[BudgetAlert]:
        return await self._store.recent(limit)
