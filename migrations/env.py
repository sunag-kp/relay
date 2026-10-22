"""Alembic environment: runs migrations over the same async engine settings as the app."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from relay.config import get_settings
from relay.infra.postgres.db import create_engine
from relay.infra.postgres.tables import metadata

target_metadata = metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_engine(get_settings())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=get_settings().async_database_url, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
