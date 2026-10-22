from __future__ import annotations

import httpx

from relay.domain.usage import BudgetAlert
from relay.observability.log import get_logger

log = get_logger("relay.alerts")


class WebhookNotifier:
    """POSTs budget alerts as JSON. The `text` field makes it work with Slack and Discord
    incoming webhooks as-is; the structured fields are there for anything else."""

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=5)

    async def notify(self, alert: BudgetAlert) -> None:
        text = (
            f"Relay budget alert: key '{alert.key_name}' ({alert.owner}) reached "
            f"{alert.threshold_pct}% of its ${alert.budget_usd} budget for {alert.period} "
            f"(spent ${alert.spend_usd:.2f})."
        )
        payload = {
            "text": text,
            "api_key_id": str(alert.api_key_id),
            "threshold_pct": alert.threshold_pct,
            "period": alert.period,
            "spend_usd": str(alert.spend_usd),
            "budget_usd": str(alert.budget_usd),
        }
        try:
            response = await self._client.post(self._url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("budget_alert_webhook_failed", error=str(exc))
