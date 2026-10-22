"""Two-layer response cache: exact match in Redis, then semantic match in pgvector."""

from __future__ import annotations

from dataclasses import dataclass

from relay.domain.cache import (
    CachedCompletion,
    exact_key,
    is_cacheable,
    semantic_namespace,
    semantic_text,
)
from relay.domain.chat import ChatCompletionRequest
from relay.observability import metrics
from relay.observability.log import get_logger
from relay.services.ports import Embedder, ExactCache, SemanticCache

log = get_logger("relay.cache")


@dataclass(frozen=True, slots=True)
class CacheHit:
    entry: CachedCompletion
    layer: str  # "exact" | "semantic"
    similarity: float | None = None


@dataclass(slots=True)
class CacheProbe:
    """Keys computed during lookup, reused when the answer is stored (embed only once)."""

    exact_key: str | None = None
    namespace: str | None = None
    embedding: list[float] | None = None


class ResponseCache:
    def __init__(
        self,
        exact: ExactCache | None,
        semantic: SemanticCache | None,
        embedder: Embedder | None,
        *,
        ttl_s: int,
        threshold: float,
    ) -> None:
        self._exact = exact
        self._semantic = semantic if embedder is not None else None
        self._embedder = embedder
        self._ttl_s = ttl_s
        self._threshold = threshold

    async def lookup(
        self, owner: str, request: ChatCompletionRequest
    ) -> tuple[CacheHit | None, CacheProbe]:
        probe = CacheProbe()
        if not is_cacheable(request):
            return None, probe

        if self._exact is not None:
            probe.exact_key = exact_key(owner, request)
            entry = await self._exact.get(probe.exact_key)
            metrics.CACHE_LOOKUPS.labels(layer="exact", result="hit" if entry else "miss").inc()
            if entry is not None:
                return CacheHit(entry, "exact"), probe

        if self._semantic is not None and self._embedder is not None:
            text = semantic_text(request)
            if text is None:
                return None, probe
            probe.namespace = semantic_namespace(owner, request, self._embedder.id)
            probe.embedding = await self._embed(text)
            if probe.embedding is None:
                return None, probe
            match = await self._semantic.nearest(probe.namespace, probe.embedding)
            hit = match is not None and match.similarity >= self._threshold
            metrics.CACHE_LOOKUPS.labels(layer="semantic", result="hit" if hit else "miss").inc()
            if match is not None and hit:
                return CacheHit(match.entry, "semantic", round(match.similarity, 4)), probe
        return None, probe

    async def store(
        self,
        owner: str,
        request: ChatCompletionRequest,
        probe: CacheProbe | None,
        entry: CachedCompletion,
    ) -> None:
        if not is_cacheable(request):
            return
        probe = probe or CacheProbe()
        if self._exact is not None:
            await self._exact.put(probe.exact_key or exact_key(owner, request), entry, self._ttl_s)
        if self._semantic is None or self._embedder is None:
            return
        if probe.embedding is None:
            text = semantic_text(request)
            if text is None:
                return
            probe.namespace = semantic_namespace(owner, request, self._embedder.id)
            probe.embedding = await self._embed(text)
        if probe.embedding is not None and probe.namespace is not None:
            await self._semantic.put(probe.namespace, probe.embedding, entry, self._ttl_s)

    async def flush(self) -> dict[str, int]:
        flushed = {"exact": 0, "semantic": 0}
        if self._exact is not None:
            flushed["exact"] = await self._exact.flush()
        if self._semantic is not None:
            flushed["semantic"] = await self._semantic.flush()
        return flushed

    async def purge_expired(self) -> int:
        return await self._semantic.purge_expired() if self._semantic is not None else 0

    async def _embed(self, text: str) -> list[float] | None:
        assert self._embedder is not None
        try:
            return await self._embedder.embed(text)
        except Exception as exc:
            log.warning("embedding_failed", embedder=self._embedder.id, error=repr(exc))
            return None
