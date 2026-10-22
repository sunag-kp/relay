from __future__ import annotations

from decimal import Decimal

import pytest

from relay.config import Settings
from relay.services.keys import KeySpec
from tests.conftest import HarnessFactory


async def test_admin_endpoints_need_the_admin_token(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    for headers in ({}, {"authorization": "Bearer wrong"}, harness.auth):
        response = await harness.client.get("/admin/keys", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "authentication_error"
    assert (await harness.client.get("/admin/keys", headers=harness.admin)).status_code == 200


async def test_key_lifecycle(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    created = await harness.client.post(
        "/admin/keys",
        json={
            "name": "search",
            "owner": "team-search",
            "rpm_limit": 3,
            "monthly_budget_usd": "25.00",
        },
        headers=harness.admin,
    )
    assert created.status_code == 201
    body = created.json()
    plaintext = body["api_key"]
    assert plaintext.startswith(body["prefix"])
    assert body["tpd_limit"] == Settings().default_tpd_limit
    key_id = body["id"]

    listed = (await harness.client.get("/admin/keys", headers=harness.admin)).json()
    assert key_id in {k["id"] for k in listed}
    assert all("api_key" not in k for k in listed)  # the secret is never shown again

    as_client = {"authorization": f"Bearer {plaintext}"}
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
    assert (
        await harness.client.post("/v1/chat/completions", json=payload, headers=as_client)
    ).status_code == 200

    patched = await harness.client.patch(
        f"/admin/keys/{key_id}",
        json={"rpm_limit": 99, "monthly_budget_usd": None},
        headers=harness.admin,
    )
    assert patched.json()["rpm_limit"] == 99
    assert patched.json()["monthly_budget_usd"] is None
    assert patched.json()["name"] == "search"  # untouched

    revoked = await harness.client.delete(f"/admin/keys/{key_id}", headers=harness.admin)
    assert revoked.json()["active"] is False
    after = await harness.client.post("/v1/chat/completions", json=payload, headers=as_client)
    assert after.status_code == 401
    assert (await harness.client.get(f"/admin/keys/{key_id}", headers=harness.admin)).json()[
        "active"
    ] is False
    assert key_id in {
        k["id"]
        for k in (
            await harness.client.get("/admin/keys?include_revoked=true", headers=harness.admin)
        ).json()
    }


async def test_unknown_models_and_keys_are_rejected(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    bad = await harness.client.post(
        "/admin/keys", json={"name": "x", "allowed_models": ["nope"]}, headers=harness.admin
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["param"] == "allowed_models"
    missing = await harness.client.get(
        "/admin/keys/00000000-0000-0000-0000-000000000000", headers=harness.admin
    )
    assert missing.status_code == 404


async def test_usage_summary_and_events_reflect_traffic(make_harness: HarnessFactory) -> None:
    harness = await make_harness(cache=True)
    for text in ("one", "two", "one"):  # the second "one" is a cache hit
        await harness.chat(messages=[{"role": "user", "content": text}])
        await harness.drain()

    summary = (
        await harness.client.get("/admin/usage/summary?days=3", headers=harness.admin)
    ).json()
    totals = summary["totals"]
    assert totals["requests"] == 3
    assert totals["cache_hits"] == 1
    assert totals["cache_hit_rate"] == pytest.approx(0.3333, abs=1e-3)
    assert totals["prompt_tokens"] == 20  # cache hits use no provider tokens
    assert totals["cost_usd"] > 0
    assert totals["saved_usd"] > 0
    assert summary["by_key"][0]["owner"] == "team-a"
    assert summary["by_route"][0]["route"] == "test-model"

    events = (await harness.client.get("/admin/usage/events?limit=2", headers=harness.admin)).json()
    assert len(events) == 2
    assert events[0]["cache"] == "exact"
    assert events[0]["replica"] == "test"


async def test_failed_requests_are_metered_as_errors(make_harness: HarnessFactory) -> None:
    from relay.domain.errors import FailureKind, ProviderFailure
    from tests.fakes import FakeProvider

    failing = ProviderFailure("x", FailureKind.SERVER, "down", status=503)
    providers = {
        "alpha": FakeProvider("alpha", behaviours=[failing]),
        "beta": FakeProvider("beta", behaviours=[failing]),
    }
    harness = await make_harness(providers=providers)
    await harness.chat()
    await harness.drain()
    [event] = harness.usage_store.events
    assert event.status == "upstream_error"
    assert event.provider is None
    assert (event.prompt_tokens, event.cost_usd) == (0, 0)


async def test_budget_alert_fires_from_real_spend(make_harness: HarnessFactory) -> None:
    spec = KeySpec(
        name="tiny",
        owner="o",
        rpm_limit=100,
        tpd_limit=10**6,
        monthly_budget_usd=Decimal("0.00003"),
    )
    harness = await make_harness(key_spec=spec)
    await harness.chat()  # costs $0.000018 = 60% of the budget
    await harness.drain()
    alerts = (await harness.client.get("/admin/alerts", headers=harness.admin)).json()
    assert [a["threshold_pct"] for a in alerts] == [50]
    assert harness.notifier.sent[0].key_name == "tiny"


async def test_public_dashboard_mode_opens_only_read_endpoints(
    make_harness: HarnessFactory,
) -> None:
    harness = await make_harness(settings=Settings(app_env="test", dashboard_public=True))
    assert (await harness.client.get("/admin/usage/summary")).status_code == 200
    assert (await harness.client.get("/admin/providers")).status_code == 200
    assert (await harness.client.get("/admin/alerts")).status_code == 200
    assert (await harness.client.get("/admin/keys")).status_code == 401
    assert (await harness.client.post("/admin/cache/flush")).status_code == 401


async def test_providers_view_merges_replicas(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    board = harness.container.health_board
    assert board is not None
    await board.publish(
        "r1",
        {
            "providers": {"alpha": {"state": "open", "failure_rate": 0.9}},
            "latency_ms": {"alpha/alpha-1": 41.0},
        },
        ttl_s=10,
    )
    await board.publish(
        "r2",
        {"providers": {"alpha": {"state": "closed", "failure_rate": 0.0}}, "latency_ms": {}},
        ttl_s=10,
    )
    body = (await harness.client.get("/admin/providers", headers=harness.admin)).json()
    alpha = next(p for p in body["providers"] if p["provider"] == "alpha")
    assert alpha["worst_state"] == "open"
    assert alpha["replicas"] == {"r1": "open", "r2": "closed"}
    assert body["replicas"] == ["r1", "r2"]
    assert body["routes"][0]["model"] == "test-model"


async def test_cache_flush(make_harness: HarnessFactory) -> None:
    harness = await make_harness(cache=True)
    await harness.chat()
    await harness.drain()
    flushed = (await harness.client.post("/admin/cache/flush", headers=harness.admin)).json()
    assert flushed == {"exact": 1, "semantic": 1}
    assert (await harness.chat()).headers["x-relay-cache"] == "miss"
