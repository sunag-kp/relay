"""The OpenAI and Anthropic adapters over real HTTP, against the mock provider and a stub."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from relay.domain.catalog import ProviderKind, ProviderSpec
from relay.domain.chat import ChatCompletionRequest, StreamAccumulator
from relay.domain.errors import FailureKind, ProviderFailure
from relay.infra.providers.anthropic import AnthropicProvider
from relay.infra.providers.http import parse_retry_after
from relay.infra.providers.openai import OpenAIProvider
from tests.servers import StubUpstream


def chat_request(**overrides: object) -> ChatCompletionRequest:
    body: dict[str, object] = {
        "model": "relay-mock",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ping pong"},
        ],
    }
    body.update(overrides)
    return ChatCompletionRequest.model_validate(body)


def openai_provider(
    base_url: str,
    kind: ProviderKind = ProviderKind.OPENAI_COMPATIBLE,
    **spec: object,
) -> OpenAIProvider:
    return OpenAIProvider(
        ProviderSpec.model_validate({"name": "mock", "kind": kind, "base_url": base_url, **spec})
    )


def anthropic_provider(base_url: str, **spec: object) -> AnthropicProvider:
    return AnthropicProvider(
        ProviderSpec.model_validate(
            {"name": "mock-claude", "kind": "anthropic", "base_url": base_url, **spec}
        )
    )


async def collect(events: AsyncIterator[dict[str, object]]) -> StreamAccumulator:
    acc = StreamAccumulator()
    async for event in events:
        acc.add(event)
    return acc


async def test_openai_complete(mock_provider_url: str) -> None:
    provider = openai_provider(f"{mock_provider_url}/p/alpha/v1")
    completion = await provider.complete(chat_request(max_tokens=8), "mock-large")
    assert completion.body["model"] == "mock-large"
    assert completion.text().startswith("[alpha] You said: ping pong")
    assert completion.usage.completion_tokens == 8
    assert completion.body["choices"][0]["finish_reason"] == "length"
    await provider.aclose()


async def test_openai_stream_reports_usage(mock_provider_url: str) -> None:
    provider = openai_provider(f"{mock_provider_url}/p/alpha/v1")
    acc = await collect(provider.stream(chat_request(), "mock-large"))
    assert acc.text.startswith("[alpha] You said: ping pong")
    assert acc.finish_reason == "stop"
    assert acc.usage is not None
    assert acc.usage.completion_tokens == len(acc.text.split())
    await provider.aclose()


async def test_openai_kind_sends_max_completion_tokens(
    stub_upstream: tuple[str, StubUpstream],
) -> None:
    url, stub = stub_upstream
    stub.reply = {
        "status": 200,
        "json": {
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    }
    provider = openai_provider(url, ProviderKind.OPENAI)
    await provider.complete(chat_request(max_tokens=5), "gpt-5-mini")
    seen = stub.requests[-1]
    assert seen["max_completion_tokens"] == 5
    assert "max_tokens" not in seen
    assert seen["model"] == "gpt-5-mini"
    await provider.aclose()


async def test_openai_missing_usage_is_estimated(stub_upstream: tuple[str, StubUpstream]) -> None:
    url, stub = stub_upstream
    stub.reply = {
        "status": 200,
        "json": {"choices": [{"message": {"content": "four words right here"}}]},
    }
    provider = openai_provider(url)
    completion = await provider.complete(chat_request(), "m")
    assert completion.usage.completion_tokens == 6  # 21 chars / 4, rounded up
    assert completion.usage.prompt_tokens > 0
    await provider.aclose()


async def test_anthropic_complete_and_stream(mock_provider_url: str) -> None:
    provider = anthropic_provider(f"{mock_provider_url}/p/claude")
    completion = await provider.complete(chat_request(temperature=0.3), "claude-haiku-5-5")
    assert completion.text().startswith("[claude] You said: ping pong")
    assert completion.body["model"] == "claude-haiku-5-5"
    assert completion.usage.prompt_tokens > 0

    acc = await collect(provider.stream(chat_request(), "claude-haiku-5-5"))
    assert acc.text == completion.text()
    assert acc.usage == completion.usage
    await provider.aclose()


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (503, FailureKind.SERVER, True),
        (529, FailureKind.SERVER, True),
        (429, FailureKind.RATE_LIMITED, True),
        (401, FailureKind.AUTH, False),
        (400, FailureKind.BAD_REQUEST, False),
    ],
)
async def test_http_errors_are_classified(
    mock_provider_url: str, status: int, kind: FailureKind, retryable: bool
) -> None:
    forced = {"headers": {"x-mock-status": str(status)}}
    for provider in (
        openai_provider(f"{mock_provider_url}/p/alpha/v1", **forced),
        anthropic_provider(f"{mock_provider_url}/p/claude", **forced),
    ):
        with pytest.raises(ProviderFailure) as info:
            await provider.complete(chat_request(), "m")
        assert info.value.kind is kind
        assert info.value.retryable is retryable
        assert info.value.status == status
        if status == 429:
            assert info.value.retry_after_s == 1.0
        # A stream that fails before its first event raises the same failure.
        with pytest.raises(ProviderFailure):
            await collect(provider.stream(chat_request(), "m"))
        await provider.aclose()


async def test_connection_refused_is_a_retryable_connection_failure() -> None:
    provider = openai_provider("http://127.0.0.1:1", connect_timeout_s=1)
    with pytest.raises(ProviderFailure) as info:
        await provider.complete(chat_request(), "m")
    assert info.value.kind is FailureKind.CONNECTION
    assert info.value.retryable
    await provider.aclose()


async def test_slow_upstream_is_a_timeout(stub_upstream: tuple[str, StubUpstream]) -> None:
    url, stub = stub_upstream
    stub.reply = {"status": 200, "json": {}, "delay_s": 1.0}
    provider = openai_provider(url, timeout_s=0.2)
    with pytest.raises(ProviderFailure) as info:
        await provider.complete(chat_request(), "m")
    assert info.value.kind is FailureKind.TIMEOUT
    await provider.aclose()


async def test_non_json_response_is_a_protocol_failure(
    stub_upstream: tuple[str, StubUpstream],
) -> None:
    url, stub = stub_upstream
    stub.reply = {"status": 200, "text": "<html>gateway</html>"}
    provider = openai_provider(url)
    with pytest.raises(ProviderFailure) as info:
        await provider.complete(chat_request(), "m")
    assert info.value.kind is FailureKind.PROTOCOL
    await provider.aclose()


async def test_error_body_message_is_kept(stub_upstream: tuple[str, StubUpstream]) -> None:
    url, stub = stub_upstream
    stub.reply = {"status": 400, "json": {"error": {"message": "context length exceeded"}}}
    provider = openai_provider(url)
    with pytest.raises(ProviderFailure) as info:
        await provider.complete(chat_request(), "m")
    assert info.value.message == "context length exceeded"
    assert info.value.is_client_error
    await provider.aclose()


def test_api_keys_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = ProviderSpec(
        name="openai", kind=ProviderKind.OPENAI, base_url="http://o", api_key_env="TEST_OPENAI_KEY"
    )
    monkeypatch.delenv("TEST_OPENAI_KEY", raising=False)
    assert not OpenAIProvider(spec).configured
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    provider = OpenAIProvider(spec)
    assert provider.configured
    assert provider._upstream.headers["authorization"] == "Bearer sk-test"


@pytest.mark.parametrize(("value", "expected"), [("2", 2.0), (None, None), ("garbage", None)])
def test_retry_after_parsing(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected
