from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Row, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.domain.errors import Conflict
from relay.domain.keys import ApiKey
from relay.infra.postgres.tables import api_keys
from relay.services.ports import KeyChanges

_COLUMNS = [c for c in api_keys.c if c.name != "key_hash"]


def _to_key(row: Row[Any]) -> ApiKey:
    allowed = row.allowed_models
    return ApiKey(
        id=row.id,
        name=row.name,
        owner=row.owner,
        prefix=row.prefix,
        rpm_limit=row.rpm_limit,
        tpd_limit=row.tpd_limit,
        monthly_budget_usd=row.monthly_budget_usd,
        enforce_budget=row.enforce_budget,
        allowed_models=tuple(allowed) if allowed is not None else None,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


class PostgresApiKeyRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(*_COLUMNS).where(api_keys.c.key_hash == key_hash))
            ).first()
        return _to_key(row) if row else None

    async def get(self, key_id: UUID) -> ApiKey | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(*_COLUMNS).where(api_keys.c.id == key_id))).first()
        return _to_key(row) if row else None

    async def hash_of(self, key_id: UUID) -> str | None:
        async with self._engine.connect() as conn:
            value = await conn.scalar(select(api_keys.c.key_hash).where(api_keys.c.id == key_id))
        return str(value) if value is not None else None

    async def insert(self, key: ApiKey, key_hash: str) -> None:
        try:
            await self._insert(key, key_hash)
        except IntegrityError as exc:
            raise Conflict("An API key with this value already exists.") from exc

    async def _insert(self, key: ApiKey, key_hash: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                api_keys.insert().values(
                    id=key.id,
                    name=key.name,
                    owner=key.owner,
                    key_hash=key_hash,
                    prefix=key.prefix,
                    rpm_limit=key.rpm_limit,
                    tpd_limit=key.tpd_limit,
                    monthly_budget_usd=key.monthly_budget_usd,
                    enforce_budget=key.enforce_budget,
                    allowed_models=list(key.allowed_models) if key.allowed_models else None,
                    created_at=key.created_at,
                )
            )

    async def list(self, *, include_revoked: bool) -> list[ApiKey]:
        query = select(*_COLUMNS).order_by(api_keys.c.created_at)
        if not include_revoked:
            query = query.where(api_keys.c.revoked_at.is_(None))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [_to_key(row) for row in rows]

    async def update(self, key_id: UUID, changes: KeyChanges) -> ApiKey | None:
        values: dict[str, Any] = {}
        if changes.name is not None:
            values["name"] = changes.name
        if changes.rpm_limit is not None:
            values["rpm_limit"] = changes.rpm_limit
        if changes.tpd_limit is not None:
            values["tpd_limit"] = changes.tpd_limit
        if changes.clear_budget:
            values["monthly_budget_usd"] = None
        elif changes.monthly_budget_usd is not None:
            values["monthly_budget_usd"] = Decimal(str(changes.monthly_budget_usd))
        if changes.enforce_budget is not None:
            values["enforce_budget"] = changes.enforce_budget
        if changes.clear_allowed_models:
            values["allowed_models"] = None
        elif changes.allowed_models is not None:
            values["allowed_models"] = changes.allowed_models
        if not values:
            return await self.get(key_id)
        return await self._update_returning(key_id, values)

    async def revoke(self, key_id: UUID) -> ApiKey | None:
        key = await self.get(key_id)
        if key is None or key.revoked_at is not None:
            return key
        return await self._update_returning(key_id, {"revoked_at": datetime.now(UTC)})

    async def _update_returning(self, key_id: UUID, values: dict[str, Any]) -> ApiKey | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    update(api_keys)
                    .where(api_keys.c.id == key_id)
                    .values(**values)
                    .returning(*_COLUMNS)
                )
            ).first()
        return _to_key(row) if row else None
