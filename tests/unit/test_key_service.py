from __future__ import annotations

import uuid

import pytest

from relay.domain.errors import AuthenticationFailed, NotFound
from relay.domain.keys import hash_key
from relay.services.keys import KeyService, KeySpec
from relay.services.ports import KeyChanges
from tests.fakes import FakeKeyCache, FakeKeyRepository

SPEC = KeySpec(name="k", owner="team", rpm_limit=10, tpd_limit=1000)


def service() -> tuple[KeyService, FakeKeyRepository, FakeKeyCache]:
    repository, cache = FakeKeyRepository(), FakeKeyCache()
    return KeyService(repository, cache), repository, cache


async def test_created_key_authenticates_and_is_cached() -> None:
    keys, repository, cache = service()
    key, plaintext = await keys.create(SPEC)
    assert plaintext.startswith("rk_")
    assert key.prefix == plaintext[:10]
    assert repository.hashes[key.id] == hash_key(plaintext)

    assert (await keys.authenticate(plaintext)).id == key.id
    assert (await keys.authenticate(plaintext)).id == key.id
    assert repository.lookups == 1  # second lookup served from the cache
    assert cache.entries[hash_key(plaintext)] == key


async def test_unknown_keys_are_negatively_cached() -> None:
    keys, repository, _ = service()
    for _ in range(3):
        with pytest.raises(AuthenticationFailed):
            await keys.authenticate("rk_does_not_exist")
    assert repository.lookups == 1


async def test_revocation_and_updates_invalidate_the_cache() -> None:
    keys, _, _ = service()
    key, plaintext = await keys.create(SPEC)
    await keys.authenticate(plaintext)

    updated = await keys.update(key.id, KeyChanges(rpm_limit=99))
    assert (await keys.authenticate(plaintext)).rpm_limit == 99 == updated.rpm_limit

    await keys.revoke(key.id)
    with pytest.raises(AuthenticationFailed) as info:
        await keys.authenticate(plaintext)
    assert info.value.code == "revoked_api_key"
    assert [k.id for k in await keys.list(include_revoked=True)] == [key.id]
    assert await keys.list() == []


async def test_ensure_is_idempotent() -> None:
    keys, repository, _ = service()
    first = await keys.ensure("rk_demo_key", SPEC)
    second = await keys.ensure("rk_demo_key", SPEC)
    assert first.id == second.id
    assert len(repository.keys) == 1
    assert (await keys.authenticate("rk_demo_key")).id == first.id


async def test_missing_keys_raise_not_found() -> None:
    keys, _, _ = service()
    missing = uuid.uuid4()
    with pytest.raises(NotFound):
        await keys.get(missing)
    with pytest.raises(NotFound):
        await keys.revoke(missing)
    with pytest.raises(NotFound):
        await keys.update(missing, KeyChanges(name="x"))
