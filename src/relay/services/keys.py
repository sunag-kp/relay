from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from relay.domain.errors import AuthenticationFailed, Conflict, NotFound
from relay.domain.keys import KEY_PREFIX, ApiKey, NewKey, display_prefix, generate_key, hash_key
from relay.services.ports import ApiKeyCache, ApiKeyRepository, KeyChanges


@dataclass(frozen=True, slots=True)
class KeySpec:
    name: str
    owner: str
    rpm_limit: int
    tpd_limit: int
    monthly_budget_usd: Decimal | None = None
    enforce_budget: bool = False
    allowed_models: tuple[str, ...] | None = None


class KeyService:
    """Authenticates gateway keys and manages their lifecycle.

    Lookups go Redis -> Postgres and are cached for `ttl` seconds in Redis (shared by every
    replica). Revoking or editing a key deletes the cached record, so all replicas see the
    change on their next request; the TTL only bounds staleness if that delete is lost.
    """

    def __init__(self, repository: ApiKeyRepository, cache: ApiKeyCache) -> None:
        self._repository = repository
        self._cache = cache

    async def authenticate(self, plaintext: str | None) -> ApiKey:
        if not plaintext or not plaintext.startswith(KEY_PREFIX):
            raise AuthenticationFailed(
                "Missing or malformed API key. Use 'Authorization: Bearer rk_...'."
            )
        key_hash = hash_key(plaintext)
        cached = await self._cache.get(key_hash)
        if cached is False:
            raise AuthenticationFailed("Invalid API key.")
        key = cached if isinstance(cached, ApiKey) else None
        if key is None:
            key = await self._repository.get_by_hash(key_hash)
            # Negative results are cached too, so a client hammering with a bad key costs
            # one Redis read per request, not one Postgres query.
            await self._cache.put(key_hash, key)
        if key is None:
            raise AuthenticationFailed("Invalid API key.")
        if not key.active:
            raise AuthenticationFailed("This API key has been revoked.", code="revoked_api_key")
        return key

    async def create(self, spec: KeySpec, *, plaintext: str | None = None) -> tuple[ApiKey, str]:
        """Create a key; returns the record and the plaintext (shown to the caller once)."""
        new = (
            generate_key()
            if plaintext is None
            else NewKey(plaintext, hash_key(plaintext), display_prefix(plaintext))
        )
        key = ApiKey(
            id=uuid.uuid4(),
            name=spec.name,
            owner=spec.owner,
            prefix=new.prefix,
            rpm_limit=spec.rpm_limit,
            tpd_limit=spec.tpd_limit,
            monthly_budget_usd=spec.monthly_budget_usd,
            enforce_budget=spec.enforce_budget,
            allowed_models=spec.allowed_models,
            created_at=datetime.now(UTC),
        )
        await self._repository.insert(key, new.key_hash)
        await self._cache.invalidate(new.key_hash)
        return key, new.plaintext

    async def ensure(self, plaintext: str, spec: KeySpec) -> ApiKey:
        """Idempotently provision a known key (the public demo key).

        Every replica runs this at startup, concurrently; the loser of the insert race
        reads the winner's row instead of failing.
        """
        key_hash = hash_key(plaintext)
        existing = await self._repository.get_by_hash(key_hash)
        if existing is not None:
            return existing
        try:
            key, _ = await self.create(spec, plaintext=plaintext)
        except Conflict:
            key = await self._repository.get_by_hash(key_hash)  # type: ignore[assignment]
            if key is None:
                raise
        return key

    async def get(self, key_id: UUID) -> ApiKey:
        key = await self._repository.get(key_id)
        if key is None:
            raise NotFound(f"No API key with id {key_id}.")
        return key

    async def list(self, *, include_revoked: bool = False) -> list[ApiKey]:
        return await self._repository.list(include_revoked=include_revoked)

    async def update(self, key_id: UUID, changes: KeyChanges) -> ApiKey:
        key = await self._repository.update(key_id, changes)
        if key is None:
            raise NotFound(f"No API key with id {key_id}.")
        await self._invalidate(key_id)
        return key

    async def revoke(self, key_id: UUID) -> ApiKey:
        key = await self._repository.revoke(key_id)
        if key is None:
            raise NotFound(f"No API key with id {key_id}.")
        await self._invalidate(key_id)
        return key

    async def _invalidate(self, key_id: UUID) -> None:
        key_hash = await self._repository.hash_of(key_id)
        if key_hash is not None:
            await self._cache.invalidate(key_hash)
