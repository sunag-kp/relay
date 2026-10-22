from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from relay.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    connect_args: dict[str, Any] = {"server_settings": {"application_name": settings.service_name}}
    url = settings.async_database_url
    if settings.database_uses_pgbouncer:
        # PgBouncer (transaction pooling) cannot route server-side prepared statements.
        connect_args["statement_cache_size"] = 0
        url += ("&" if "?" in url else "?") + "prepared_statement_cache_size=0"
    return create_async_engine(
        url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_size,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args=connect_args,
    )


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
