from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.domain.cache import CachedCompletion
from relay.infra.postgres.tables import semantic_cache
from relay.observability.log import get_logger
from relay.services.ports import SemanticMatch

log = get_logger("relay.cache")


class PostgresSemanticCache:
    """Nearest-neighbour lookup over cached answers with pgvector (HNSW, cosine distance).

    Living in Postgres rather than in each replica's memory means every replica shares one
    semantic cache, and the managed free tiers (Neon) support pgvector out of the box.
    """

    def __init__(self, engine: AsyncEngine, *, ef_search: int = 64) -> None:
        self._engine = engine
        self._ef_search = ef_search

    async def nearest(self, namespace: str, embedding: Sequence[float]) -> SemanticMatch | None:
        distance = semantic_cache.c.embedding.cosine_distance(list(embedding))
        query = (
            select(semantic_cache.c.response, distance.label("distance"))
            .where(semantic_cache.c.namespace == namespace)
            .where(semantic_cache.c.expires_at > func.now())
            .order_by(distance)
            .limit(1)
        )
        try:
            async with self._engine.begin() as conn:
                # The namespace filter is applied while walking the HNSW graph; iterative
                # scans (pgvector >= 0.8) keep walking until a matching row is found.
                await conn.execute(
                    text(
                        "SELECT set_config('hnsw.ef_search', :ef, true),"
                        " set_config('hnsw.iterative_scan', 'relaxed_order', true)"
                    ),
                    {"ef": str(self._ef_search)},
                )
                row = (await conn.execute(query)).first()
        except SQLAlchemyError as exc:
            log.warning("semantic_cache_lookup_failed", error=str(exc))
            return None
        if row is None:
            return None
        return SemanticMatch(
            entry=CachedCompletion.from_json(row.response), similarity=1.0 - float(row.distance)
        )

    async def put(
        self, namespace: str, embedding: Sequence[float], entry: CachedCompletion, ttl_s: int
    ) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(semantic_cache).values(
                        namespace=namespace,
                        embedding=list(embedding),
                        response=entry.to_json(),
                        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_s),
                    )
                )
        except SQLAlchemyError as exc:
            log.warning("semantic_cache_put_failed", error=str(exc))

    async def purge_expired(self) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(semantic_cache).where(semantic_cache.c.expires_at <= func.now())
            )
        return int(result.rowcount or 0)

    async def flush(self) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(delete(semantic_cache))
        return int(result.rowcount or 0)
