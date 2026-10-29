"""The cache contract: what every implementation must do, identically."""

import pytest

from firm.core.remote.cache import (
    CacheEntry,
    MemoryRemoteCache,
    RemoteCache,
    SqliteRemoteCache,
)

DOCUMENT = {"id": "http://remote.test/note/1", "type": "Note"}
OTHER_URI = "http://remote.test/note/2"

BACKENDS = ["memory", "sqlite"]


def _make(kind: str, tmp_path) -> RemoteCache:
    if kind == "memory":
        return MemoryRemoteCache()
    if kind == "sqlite":
        return SqliteRemoteCache(tmp_path / "cache.sqlite")
    raise AssertionError(f"unknown cache kind: {kind}")


@pytest.fixture(params=BACKENDS)
def cache(request, tmp_path) -> RemoteCache:
    return _make(request.param, tmp_path)


def entry(
    uri: str = "http://remote.test/note/1",
    document: dict | None = DOCUMENT,
    status_code: int | None = 200,
    now: float = 1_000.0,
    ttl: float = 300.0,
) -> CacheEntry:
    return CacheEntry(
        uri=uri,
        document=document,
        status_code=status_code,
        fetched_at=now,
        expires_at=now + ttl,
    )


async def test_put_get_roundtrip(cache):
    stored = entry()

    await cache.put(stored)

    assert await cache.get(stored.uri) == stored


async def test_negative_entry_roundtrip(cache):
    stored = entry(document=None, status_code=404)

    await cache.put(stored)
    got = await cache.get(stored.uri)

    assert got is not None
    assert got.document is None
    assert got.status_code == 404


async def test_put_overwrites_existing_entry(cache):
    await cache.put(entry())
    replacement = entry(document={"id": "http://remote.test/note/1", "content": "second"})

    await cache.put(replacement)

    got = await cache.get(replacement.uri)
    assert got is not None
    assert got.document == replacement.document


async def test_delete_removes_entry(cache):
    stored = entry()
    await cache.put(stored)

    await cache.delete(stored.uri)

    assert await cache.get(stored.uri) is None


async def test_purge_expired_removes_only_expired(cache):
    stale = entry(uri="http://remote.test/note/1", now=1_000.0, ttl=10.0)
    fresh = entry(uri=OTHER_URI, now=1_000.0, ttl=300.0)
    await cache.put(stale)
    await cache.put(fresh)

    assert await cache.purge_expired(1_100.0) == 1

    assert await cache.get(stale.uri) is None
    assert await cache.get(fresh.uri) is not None
