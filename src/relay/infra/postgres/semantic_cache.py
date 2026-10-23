from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, delete, func, insert, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.domain.cache import CachedCompletion
from relay.infra.postgres.tables import EMBEDDING_DIMENSIONS, semantic_cache
from relay.observability.log import get_logger
from relay.services.ports import SemanticMatch

log = get_logger("relay.cache")

_distance = semantic_cache.c.embedding.cosine_distance(
    bindparam("embedding", type_=Vector(EMBEDDING_DIMENSIONS))
)
# Built once: SQLAlchemy then only binds parameters per call instead of constructing and
# cache-keying a new statement every time.
_NEAREST = (
    select(semantic_cache.c.response, _distance.label("distance"))
    .where(semantic_cache.c.namespace == bindparam("namespace"))
    .where(semantic_cache.c.expires_at > func.now())
    .order_by(_distance)
    .limit(1)
)
_INSERT = insert(semantic_cache)


class PostgresSemanticCache:
    """Nearest-neighbour lookup over cached answers with pgvector (HNSW, cosine distance).

    Living in Postgres rather than in each replica's memory means every replica shares one
    semantic cache, and the managed free tiers (Neon) support pgvector out of the box.

    The namespace filter is applied while walking the HNSW graph; iterative scans (pgvector
    >= 0.8, enabled as a database default by migration 0004) keep walking until a matching
    row is found. Lookups and inserts are single statements run in autocommit mode: one
    round trip each, with no BEGIN/COMMIT around them.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine.execution_options(isolation_level="AUTOCOMMIT")

    async def nearest(self, namespace: str, embedding: Sequence[float]) -> SemanticMatch | None:
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    _NEAREST, {"namespace": namespace, "embedding": list(embedding)}
                )
                row = result.first()
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
            async with self._engine.connect() as conn:
                await conn.execute(
                    _INSERT,
                    {
                        "namespace": namespace,
                        "embedding": list(embedding),
                        "response": entry.to_json(),
                        "expires_at": datetime.now(UTC) + timedelta(seconds=ttl_s),
                    },
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
        # TRUNCATE rather than DELETE: deleted rows would leave autovacuum to repair the
        # HNSW graph row by row, which competes with live traffic for Postgres CPU.
        async with self._engine.begin() as conn:
            count = (await conn.execute(select(func.count()).select_from(semantic_cache))).scalar()
            await conn.execute(text("TRUNCATE semantic_cache"))
        return int(count or 0)
