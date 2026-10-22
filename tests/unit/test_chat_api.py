from __future__ import annotations

import json

import httpx
import pytest

from relay.domain.errors import FailureKind, ProviderFailure
from tests.conftest import HarnessFactory, make_catalog
from tests.fakes import FakeProvider, make_completion


def sse_events(response: httpx.Response) -> list[dict[str, object] | str]:
    events: list[dict[str, object] | str] = []
    for block in response.text.split("\n\n"):
        if not block.startswith("data: "):
            continue
        data = block.removeprefix("data: ")
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


async def test_non_streaming_completion_passes_through(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    harness.providers["alpha"].behaviours = [make_completion("pong", model="alpha-1")]

    response = await harness.chat()

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "pong"
    assert response.headers["x-relay-provider"] == "alpha"
    assert response.headers["x-relay-provider-model"] == "alpha-1"
    assert response.headers["x-relay-route"] == "test-model"
    assert harness.providers["alpha"].calls == ["complete:alpha-1"]


async def test_streaming_is_server_sent_events_ending_in_done(make_harness: HarnessFactory) -> None:
    harness = await make_harness()

    response = await harness.chat(stream=True)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response)
    assert events[-1] == "[DONE]"
    chunks = [e for e in events if isinstance(e, dict)]
    text = "".join(
        str(c["choices"][0]["delta"].get("content", ""))  # type: ignore[index]
        for c in chunks
        if c["choices"]
    )
    assert text == "hello from the fake"
    # The gateway always asks upstream for usage (to meter it) but only forwards it when
    # the client asked for it.
    assert all("usage" not in c for c in chunks)


async def test_streaming_usage_is_forwarded_when_requested(make_harness: HarnessFactory) -> None:
    harness = await make_harness()

    response = await harness.chat(stream=True, stream_options={"include_usage": True})

    chunks = [e for e in sse_events(response) if isinstance(e, dict)]
    assert chunks[-1]["usage"] == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
    assert chunks[-1]["choices"] == []


async def test_failure_before_first_chunk_is_a_proper_http_error(
    make_harness: HarnessFactory,
) -> None:
    catalog = make_catalog(targets=[{"provider": "alpha", "model": "alpha-1"}])
    alpha = FakeProvider(
        "alpha", behaviours=[ProviderFailure("alpha", FailureKind.SERVER, "boom", status=500)]
    )
    harness = await make_harness(
        catalog=catalog, providers={"alpha": alpha, "beta": FakeProvider("beta")}
    )

    response = await harness.chat(stream=True)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


async def test_failure_mid_stream_becomes_an_error_event(make_harness: HarnessFactory) -> None:
    catalog = make_catalog(targets=[{"provider": "alpha", "model": "alpha-1"}])
    alpha = FakeProvider("alpha", stream_failure_after=2)
    harness = await make_harness(
        catalog=catalog, providers={"alpha": alpha, "beta": FakeProvider("beta")}
    )

    response = await harness.chat(stream=True)

    assert response.status_code == 200
    events = sse_events(response)
    assert events[-1] == "[DONE]"
    error = events[-2]
    assert isinstance(error, dict)
    assert error["error"]["code"] == "upstream_error"  # type: ignore[index]


async def test_provider_rejecting_the_request_is_a_400(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    harness.providers["alpha"].behaviours = [
        ProviderFailure.from_status("alpha", 400, "context length exceeded")
    ]

    response = await harness.chat()

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "upstream_rejected"
    assert "context length exceeded" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({}, "invalid_api_key"),
        ({"authorization": "Bearer nope"}, "invalid_api_key"),
        ({"authorization": "Bearer rk_unknown"}, "invalid_api_key"),
    ],
)
async def test_requests_without_a_valid_key_are_401(
    make_harness: HarnessFactory, headers: dict[str, str], code: str
) -> None:
    harness = await make_harness()
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert response.status_code == 401
    assert response.json()["error"] == {
        "message": response.json()["error"]["message"],
        "type": "authentication_error",
        "code": code,
        "param": None,
    }


async def test_revoked_key_is_rejected(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    key = next(iter(harness.repository.keys.values()))
    await harness.container.keys.revoke(key.id)

    response = await harness.chat()

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "revoked_api_key"


async def test_unknown_model_is_404_in_openai_format(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    response = await harness.chat(model="gpt-unknown")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert response.json()["error"]["param"] == "model"


async def test_invalid_body_is_400_not_422(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    response = await harness.client.post(
        "/v1/chat/completions", json={"model": "test-model", "messages": []}, headers=harness.auth
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert response.json()["error"]["param"] == "messages"


async def test_unconfigured_providers_are_skipped(make_harness: HarnessFactory) -> None:
    providers = {"alpha": FakeProvider("alpha", configured=False), "beta": FakeProvider("beta")}
    harness = await make_harness(providers=providers)

    response = await harness.chat()

    assert response.status_code == 200
    assert response.headers["x-relay-provider"] == "beta"


async def test_no_configured_provider_is_503(make_harness: HarnessFactory) -> None:
    providers = {
        "alpha": FakeProvider("alpha", configured=False),
        "beta": FakeProvider("beta", configured=False),
    }
    harness = await make_harness(providers=providers)
    response = await harness.chat()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"


async def test_request_no_target_supports_is_400(make_harness: HarnessFactory) -> None:
    providers = {
        "alpha": FakeProvider("alpha", capable=False),
        "beta": FakeProvider("beta", capable=False),
    }
    harness = await make_harness(providers=providers)
    response = await harness.chat()
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameters"


async def test_key_restricted_to_other_models_gets_403(make_harness: HarnessFactory) -> None:
    from relay.services.ports import KeyChanges

    harness = await make_harness()
    key = next(iter(harness.repository.keys.values()))
    await harness.container.keys.update(key.id, KeyChanges(allowed_models=["other-model"]))

    response = await harness.chat()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_not_allowed"


async def test_bad_policy_header_is_400(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        headers={**harness.auth, "x-relay-policy": "random"},
    )
    assert response.status_code == 400


async def test_models_endpoint_lists_routes(make_harness: HarnessFactory) -> None:
    harness = await make_harness()
    response = await harness.client.get("/v1/models", headers=harness.auth)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data[0]["id"] == "test-model"
    assert data[0]["targets"] == ["alpha/alpha-1", "beta/beta-1"]
