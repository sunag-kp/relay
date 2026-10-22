from __future__ import annotations

import pytest

from relay.domain.chat import ChatCompletionRequest
from relay.domain.errors import ProviderFailure
from relay.infra.providers.anthropic import (
    DEFAULT_MAX_TOKENS,
    AnthropicProvider,
    StreamTranslator,
    from_anthropic,
    to_anthropic,
)


def request(**overrides: object) -> ChatCompletionRequest:
    body: dict[str, object] = {
        "model": "claude",
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Hi"},
            {"role": "user", "content": [{"type": "text", "text": "there"}]},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "Bye"},
        ],
    }
    body.update(overrides)
    return ChatCompletionRequest.model_validate(body)


def test_request_translation() -> None:
    body = to_anthropic(
        request(temperature=0.2, top_p=0.9, stop="END", user="u-1"), "claude-haiku-5-5"
    )
    assert body == {
        "model": "claude-haiku-5-5",
        "max_tokens": DEFAULT_MAX_TOKENS,
        "system": "Be terse.",
        # consecutive user turns are merged: the Messages API alternates roles
        "messages": [
            {"role": "user", "content": "Hi\n\nthere"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "Bye"},
        ],
        "stop_sequences": ["END"],
        "metadata": {"user_id": "u-1"},
    }
    # API version 2026-10-21 rejects sampling parameters, so they are never forwarded.
    assert "temperature" not in body
    assert "top_p" not in body


def test_max_tokens_prefers_max_completion_tokens() -> None:
    assert to_anthropic(request(max_tokens=50, max_completion_tokens=80), "m")["max_tokens"] == 80
    assert to_anthropic(request(max_tokens=50), "m")["max_tokens"] == 50


def test_response_translation() -> None:
    completion = from_anthropic(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [
                {"type": "thinking", "thinking": ""},
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7},
        },
        "anthropic",
    )
    assert completion.body["id"] == "chatcmpl-msg_1"
    assert completion.body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Hello world",
    }
    assert completion.body["choices"][0]["finish_reason"] == "length"
    assert completion.usage.prompt_tokens == 15
    assert completion.usage.completion_tokens == 7
    assert completion.body["usage"]["total_tokens"] == 22


def test_unexpected_response_is_a_protocol_failure() -> None:
    with pytest.raises(ProviderFailure) as info:
        from_anthropic({"type": "error"}, "anthropic")
    assert info.value.kind == "protocol"


def test_stream_translation() -> None:
    translator = StreamTranslator("anthropic")
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_2", "model": "claude", "usage": {"input_tokens": 12}},
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        },
        {"type": "ping"},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": " there"},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]
    out = [chunk for event in events for chunk in translator.feed(event)]
    assert translator.done
    assert [c["choices"][0]["delta"] for c in out[:3]] == [
        {"role": "assistant", "content": ""},
        {"content": "Hi"},
        {"content": " there"},
    ]
    assert out[3]["choices"][0]["finish_reason"] == "stop"
    assert out[4]["choices"] == []
    assert out[4]["usage"] == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    assert {c["id"] for c in out} == {"chatcmpl-msg_2"}


def test_stream_error_event_raises() -> None:
    translator = StreamTranslator("anthropic")
    with pytest.raises(ProviderFailure) as info:
        translator.feed({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    assert info.value.retryable


@pytest.mark.parametrize(
    ("overrides", "supported"),
    [
        ({}, True),
        ({"tools": [{"type": "function", "function": {"name": "f"}}]}, False),
        ({"n": 2}, False),
        (
            {
                "messages": [
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
                ]
            },
            False,
        ),
        ({"messages": [{"role": "tool", "content": "42", "tool_call_id": "c1"}]}, False),
    ],
)
def test_capabilities(overrides: dict[str, object], supported: bool) -> None:
    from relay.domain.catalog import ProviderKind, ProviderSpec

    provider = AnthropicProvider(
        ProviderSpec(name="a", kind=ProviderKind.ANTHROPIC, base_url="http://a")
    )
    assert provider.supports(request(**overrides)) is supported
