from __future__ import annotations

from decimal import Decimal

import pytest

from relay.domain.cache import (
    CachedCompletion,
    exact_key,
    is_storable,
    number_signature,
    semantic_namespace,
    semantic_text,
)
from relay.domain.chat import ChatCompletionRequest, Usage
from relay.infra.embeddings import HashingEmbedder, cosine
from tests.fakes import make_completion


def req(**body: object) -> ChatCompletionRequest:
    payload: dict[str, object] = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    payload.update(body)
    return ChatCompletionRequest.model_validate(payload)


def test_exact_key_ignores_transport_fields_but_not_generation_params() -> None:
    base = exact_key("team", req(temperature=0))
    assert exact_key("team", req(temperature=0, stream=True, user="u1")) == base
    assert exact_key("team", req(temperature=0.5)) != base
    assert exact_key("team", req(temperature=0, max_tokens=5)) != base
    assert exact_key("other-team", req(temperature=0)) != base  # tenants never share entries


def test_semantic_namespace_pins_everything_but_the_wording() -> None:
    a = semantic_namespace("team", req(messages=[{"role": "user", "content": "a"}]), "emb", "a")
    b = semantic_namespace("team", req(messages=[{"role": "user", "content": "b"}]), "emb", "b")
    assert a == b
    assert semantic_namespace("team", req(max_tokens=9), "emb", "a") != a
    assert semantic_namespace("team", req(), "other-embedder", "a") != a
    # different numbers: different namespace, so the two are never compared
    assert semantic_namespace("team", req(), "emb", "HTTP 429") != semantic_namespace(
        "team", req(), "emb", "HTTP 503"
    )
    assert semantic_namespace("team", req(), "emb", "100 F to C") == semantic_namespace(
        "team", req(), "emb", "C from 100 F"
    )


def test_number_signature() -> None:
    assert number_signature("convert 1,000.5 km and 40 miles") == "1000.5,40"
    assert number_signature("no digits here") == ""


@pytest.mark.parametrize(
    ("body", "eligible"),
    [
        ({}, True),
        ({"tools": [{"type": "function", "function": {"name": "f"}}]}, False),
        ({"n": 2}, False),
        ({"messages": [{"role": "user", "content": "x" * 5000}]}, False),
        (
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]},
            False,
        ),
    ],
)
def test_semantic_eligibility(body: dict[str, object], eligible: bool) -> None:
    assert (semantic_text(req(**body)) is not None) is eligible


def test_only_finished_single_choice_answers_are_stored() -> None:
    completion = make_completion("ok")
    assert is_storable(completion)
    completion.body["choices"][0]["finish_reason"] = "content_filter"
    assert not is_storable(completion)


def test_cached_entries_round_trip_and_get_fresh_ids() -> None:
    completion = make_completion("cached answer")
    entry = CachedCompletion(completion.body, Usage(5, 2), "alpha", "alpha-1", Decimal("0.0012"))
    restored = CachedCompletion.from_json(entry.to_json())
    assert restored == entry
    fresh = restored.fresh_completion()
    assert fresh.text() == "cached answer"
    assert fresh.body["id"] != completion.body["id"]


def test_hashing_embedder_matches_near_duplicates_not_different_questions() -> None:
    embedder = HashingEmbedder()
    base = embedder.embed_sync("What is the capital of France?")
    assert len(base) == 384
    assert cosine(base, base) == pytest.approx(1.0)
    assert cosine(base, embedder.embed_sync("what is the capital of france")) > 0.99
    assert cosine(base, embedder.embed_sync("What's the capital of France?")) > 0.8
    assert cosine(base, embedder.embed_sync("Explain how TCP congestion control works")) < 0.3
    # deterministic across calls (and processes: crc32, not Python's salted hash)
    assert embedder.embed_sync("same text") == embedder.embed_sync("same text")
