from __future__ import annotations

from typing import Any

from relay.domain.errors import FailureKind, ProviderFailure
from relay.services.keys import KeySpec
from tests.conftest import HarnessFactory
from tests.fakes import FakeProvider
from tests.unit.test_chat_api import sse_events


def ask(text: str, **extra: Any) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}], **extra}


async def test_identical_request_is_an_exact_hit(make_harness: HarnessFactory) -> None:
    harness = await make_harness(cache=True)
    first = await harness.chat(**ask("What is the capital of France?"))
    await harness.drain()
    second = await harness.chat(**ask("What is the capital of France?"))

    assert first.headers["x-relay-cache"] == "miss"
    assert second.headers["x-relay-cache"] == "exact"
    assert second.headers["x-relay-provider"] == "alpha"  # who originally answered
    assert second.json()["choices"] == first.json()["choices"]
    assert second.json()["id"] != first.json()["id"]
    assert harness.providers["alpha"].calls == ["complete:alpha-1"]


async def test_near_duplicate_is_a_semantic_hit(make_harness: HarnessFactory) -> None:
    harness = await make_harness(cache=True, threshold=0.8)
    await harness.chat(**ask("What is the capital of France?"))
    await harness.drain()

    near = await harness.chat(**ask("what is the capital of france"))
    assert near.headers["x-relay-cache"] == "semantic"
    assert float(near.headers["x-relay-cache-similarity"]) > 0.9
    # The semantic hit was promoted to the exact tier: repeating it is now an exact hit.
    again = await harness.chat(**ask("what is the capital of france"))
    assert again.headers["x-relay-cache"] == "exact"
    assert again.json()["choices"] == near.json()["choices"]

    different = await harness.chat(**ask("How do circuit breakers work?"))
    assert different.headers["x-relay-cache"] == "miss"


async def test_cache_hits_still_count_against_rate_limits_but_refund_tokens(
    make_harness: HarnessFactory,
) -> None:
    harness = await make_harness(
        cache=True, key_spec=KeySpec(name="k", owner="o", rpm_limit=2, tpd_limit=10**6)
    )
    await harness.chat()
    await harness.drain()
    assert (await harness.chat()).headers["x-relay-cache"] == "exact"
    await harness.drain()
    assert (await harness.chat()).status_code == 429
    hit_settlement = harness.limiter.settled[-1]
    assert hit_settlement[1:] == (0, 0)


async def test_cache_control_bypasses_and_skips_storing(make_harness: HarnessFactory) -> None:
    harness = await make_harness(cache=True)
    no_store = {**harness.auth, "cache-control": "no-store"}
    body = {"model": "test-model", **ask("fresh please")}
    await harness.client.post("/v1/chat/completions", json=body, headers=no_store)
    await harness.drain()
    assert (await harness.chat(**ask("fresh please"))).headers["x-relay-cache"] == "miss"
    await harness.drain()

    no_cache = {**harness.auth, "cache-control": "no-cache"}
    response = await harness.client.post("/v1/chat/completions", json=body, headers=no_cache)
    assert response.headers["x-relay-cache"] == "bypass"
    assert len(harness.providers["alpha"].calls) == 3


async def test_tenants_do_not_share_cache_entries(make_harness: HarnessFactory) -> None:
    harness = await make_harness(cache=True)
    await harness.chat(**ask("shared question"))
    await harness.drain()
    _, other = await harness.container.keys.create(
        KeySpec(name="b", owner="team-b", rpm_limit=10, tpd_limit=10**6)
    )
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": "test-model", **ask("shared question")},
        headers={"authorization": f"Bearer {other}"},
    )
    assert response.headers["x-relay-cache"] == "miss"


async def test_streamed_answers_are_cached_and_replayed_as_streams(
    make_harness: HarnessFactory,
) -> None:
    harness = await make_harness(cache=True)
    streamed = await harness.chat(stream=True, **ask("stream me"))
    assert streamed.headers["x-relay-cache"] == "miss"
    await harness.drain()

    replay = await harness.chat(
        stream=True, stream_options={"include_usage": True}, **ask("stream me")
    )
    assert replay.headers["x-relay-cache"] == "exact"
    events = sse_events(replay)
    assert events[-1] == "[DONE]"
    text = "".join(
        e["choices"][0]["delta"].get("content", "")
        for e in events
        if isinstance(e, dict) and e["choices"]  # type: ignore[index, union-attr]
    )
    assert text == "hello from the fake"
    assert events[-2]["usage"]["completion_tokens"] == 4  # type: ignore[index]

    as_json = await harness.chat(**ask("stream me"))
    assert as_json.headers["x-relay-cache"] == "exact"
    assert as_json.json()["choices"][0]["message"]["content"] == "hello from the fake"


async def test_failures_are_never_cached(make_harness: HarnessFactory) -> None:
    failing = ProviderFailure("x", FailureKind.SERVER, "down", status=503)
    providers = {
        "alpha": FakeProvider("alpha", behaviours=[failing]),
        "beta": FakeProvider("beta", behaviours=[failing]),
    }
    harness = await make_harness(cache=True, providers=providers)
    assert (await harness.chat()).status_code == 502
    await harness.drain()
    harness.providers["alpha"].behaviours = []
    assert (await harness.chat()).headers["x-relay-cache"] == "miss"


async def test_interrupted_streams_are_not_cached(make_harness: HarnessFactory) -> None:
    providers = {
        "alpha": FakeProvider("alpha", stream_failure_after=2),
        "beta": FakeProvider("beta"),
    }
    harness = await make_harness(cache=True, providers=providers)
    await harness.chat(stream=True)
    await harness.drain()
    assert (await harness.chat()).headers["x-relay-cache"] == "miss"
