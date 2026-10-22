"""Text embedders for the semantic cache. All produce 384-dimensional unit vectors, the
width of the `semantic_cache.embedding` column."""

from __future__ import annotations

import asyncio
import itertools
import math
import os
import re
import zlib
from pathlib import Path
from typing import Any, ClassVar

import httpx

DIMENSIONS = 384
_WORD = re.compile(r"[a-z0-9]+")


# Function words carry little meaning, so politeness and phrasing changes ("please tell me
# what ... is") barely move the vector, while content words and numbers dominate it.
_STOPWORDS = frozenset(
    "a an the is are was were be of to in on for at by with and or what whats how do does "
    "can could would you me please tell give i my your it its this that there their about "
    "into user system assistant developer".split()
)


class HashingEmbedder:
    """Dependency-free lexical embedder: feature hashing of weighted words, word bigrams and
    character trigrams into 384 signed buckets.

    Not a language model: it recognises near-duplicates (casing, punctuation, spacing,
    filler words, small edits), not paraphrases with different words. Bigrams make it order
    aware, so "convert 100 F to C" and "convert 100 C to F" do not look identical; numbers
    weigh double because changing one changes the answer. The weights and the default
    threshold come from `benchmarks/semantic_threshold.py`.

    It is the default because it needs no model download, costs well under a millisecond
    and fits a free-tier instance's memory; use `fastembed` or `openai` for meaning-level
    matching.
    """

    id = "hashing-v2"
    weights: ClassVar[dict[str, float]] = {
        "word": 1.0,
        "stopword": 0.25,
        "number": 2.0,
        "bigram": 1.0,
        "trigram": 0.25,
    }

    def embed_sync(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSIONS
        w = self.weights

        def add(feature: str, weight: float) -> None:
            h = zlib.crc32(feature.encode())
            vector[h % DIMENSIONS] += weight if (h >> 16) & 1 else -weight

        content: list[str] = []
        for word in _WORD.findall(text.lower()):
            if word in _STOPWORDS:
                add("u:" + word, w["stopword"])
                continue
            add("u:" + word, w["number"] if word.isdigit() else w["word"])
            content.append(word)
            padded = f"<{word}>"
            for i in range(len(padded) - 2):
                add("t:" + padded[i : i + 3], w["trigram"])
        for first, second in itertools.pairwise(content):
            add(f"b:{first}_{second}", w["bigram"])
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    async def embed(self, text: str) -> list[float]:
        return self.embed_sync(text)


class FastEmbedEmbedder:
    """Local ONNX sentence embedder (default BAAI/bge-small-en-v1.5, 384 dims, CPU).

    Inference runs in a worker thread so the event loop keeps serving other requests.
    Needs the `local-embeddings` extra and the model files (downloaded on first use, or
    baked into the image)."""

    def __init__(self, model: str, cache_dir: Path | None = None) -> None:
        from fastembed import TextEmbedding  # noqa: PLC0415 - optional dependency

        self.id = f"fastembed:{model}"
        self._model = TextEmbedding(
            model_name=model, cache_dir=str(cache_dir) if cache_dir else None
        )
        self._lock = asyncio.Lock()  # ONNX sessions are not re-entrant across threads here

    def embed_sync(self, text: str) -> list[float]:
        vector = next(iter(self._model.embed([text])))
        values = [float(v) for v in vector]
        if len(values) != DIMENSIONS:
            raise ValueError(f"{self.id} produced {len(values)} dims, expected {DIMENSIONS}")
        return values

    async def embed(self, text: str) -> list[float]:
        async with self._lock:
            return await asyncio.to_thread(self.embed_sync, text)


class OpenAIEmbedder:
    """OpenAI `text-embedding-3-small`, shortened to 384 dimensions (a native API option).

    Meaning-level matching without local compute, at the price of a network round trip on
    every cache lookup."""

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.id = f"openai:{model}:{DIMENSIONS}"
        self._model = model
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers={"authorization": f"Bearer {key}"}, timeout=10
        )

    async def embed(self, text: str) -> list[float]:
        response = await self._client.post(
            "/embeddings", json={"model": self._model, "input": text, "dimensions": DIMENSIONS}
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return [float(v) for v in data["data"][0]["embedding"]]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
