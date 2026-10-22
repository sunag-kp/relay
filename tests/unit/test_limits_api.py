from __future__ import annotations

from decimal import Decimal

from relay.config import Settings
from relay.domain.errors import FailureKind, ProviderFailure
from relay.services.keys import KeySpec
from tests.conftest import HarnessFactory
from tests.fakes import FakeProvider


async def test_success_carries_rate_limit_headers(make_harness: HarnessFactory) -> None:
    harness = await make_harness(
        key_spec=KeySpec(name="k", owner="o", rpm_limit=5, tpd_limit=10_000)
    )
    response = await harness.chat()
    assert response.headers["x-ratelimit-limit-requests"] == "5"
    assert response.headers["x-ratelimit-remaining-requests"] == "4"
    assert response.headers["x-ratelimit-limit-tokens"] == "10000"
    assert int(response.headers["x-ratelimit-remaining-tokens"]) < 10_000


async def test_requests_over_the_limit_get_429_with_retry_after(
    make_harness: HarnessFactory,
) -> None:
    harness = await make_harness(
        key_spec=KeySpec(name="k", owner="o", rpm_limit=2, tpd_limit=10_000)
    )
    statuses = [(await harness.chat()).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    response = await harness.chat()
    assert response.headers["retry-after"] == "2"  # ceil(1.5)
    assert response.headers["x-ratelimit-remaining-requests"] == "0"
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert response.json()["error"]["code"] == "rate_limit_exceeded"


async def test_token_quota(make_harness: HarnessFactory) -> None:
    harness = await make_harness(
        key_spec=KeySpec(name="k", owner="o", rpm_limit=100, tpd_limit=600)
    )
    assert (await harness.chat(max_tokens=250)).status_code == 200
    assert (await harness.chat(max_tokens=250)).status_code == 200
    response = await harness.chat(max_tokens=250)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "token_quota_exceeded"


async def test_request_larger_than_the_whole_quota_is_400(make_harness: HarnessFactory) -> None:
    harness = await make_harness(
        key_spec=KeySpec(name="k", owner="o", rpm_limit=100, tpd_limit=100)
    )
    response = await harness.chat(max_tokens=500)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "request_too_large"


async def test_enforced_budget(make_harness: HarnessFactory) -> None:
    spec = KeySpec(
        name="k",
        owner="o",
        rpm_limit=100,
        tpd_limit=10**6,
        monthly_budget_usd=Decimal("0.00001"),
        enforce_budget=True,
    )
    harness = await make_harness(key_spec=spec)
    assert (await harness.chat()).status_code == 200
    await harness.drain()  # settlement (which adds the spend) runs in the background
    response = await harness.chat()
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "insufficient_quota"
    assert "$0.00" in response.json()["error"]["message"]


async def test_success_settles_real_usage_and_cost(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    await harness.chat()
    await harness.drain()
    [(_, tokens, spend)] = harness.limiter.settled
    assert tokens == 14  # the fake's usage: 10 prompt + 4 completion
    assert spend == 18  # 10 * $1/M + 4 * $2/M = $0.000018


async def test_failed_request_refunds_its_tokens(make_harness: HarnessFactory) -> None:
    failing = ProviderFailure("x", FailureKind.SERVER, "down", status=503)
    providers = {
        "alpha": FakeProvider("alpha", behaviours=[failing]),
        "beta": FakeProvider("beta", behaviours=[failing]),
    }
    harness = await make_harness(providers=providers)
    assert (await harness.chat()).status_code == 502
    await harness.drain()
    assert harness.limiter.settled[0][1:] == (0, 0)
    key_id = next(iter(harness.limiter.tokens))
    assert harness.limiter.tokens[key_id] == 0
    assert harness.limiter.requests[key_id] == 1


async def test_stream_is_settled_when_it_ends(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    response = await harness.chat(stream=True)
    assert response.status_code == 200
    await harness.drain()
    assert harness.limiter.settled[0][1] == 14


async def test_limiter_outage_fails_open_by_default(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    harness.limiter.down = True
    response = await harness.chat()
    assert response.status_code == 200
    assert "x-ratelimit-remaining-requests" not in response.headers


async def test_limiter_outage_can_fail_closed(make_harness: HarnessFactory) -> None:
    harness = await make_harness(settings=Settings(app_env="test", rate_limit_fail_open=False))
    harness.limiter.down = True
    response = await harness.chat()
    assert response.status_code == 503
