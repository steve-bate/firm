"""The cache contract: what every implementation must do, identically."""

import json

import aiosqlite
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


def _make(kind: str, tmp_path, *, max_entries: int | None = None) -> RemoteCache:
    if kind == "memory":
        return MemoryRemoteCache() if max_entries is None else MemoryRemoteCache(max_entries)
    if kind == "sqlite":
        path = tmp_path / "cache.sqlite"
        return (
            SqliteRemoteCache(path) if max_entries is None else SqliteRemoteCache(path, max_entries)
        )
    raise AssertionError(f"unknown cache kind: {kind}")


@pytest.fixture(params=BACKENDS)
def cache(request, tmp_path) -> RemoteCache:
    return _make(request.param, tmp_path)


@pytest.fixture(params=BACKENDS)
def small_cache(request, tmp_path) -> RemoteCache:
    """A cache that holds two entries, so eviction is observable."""
    return _make(request.param, tmp_path, max_entries=2)


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


async def test_put_evicts_the_least_recently_used_entry(small_cache):
    first = entry(uri="http://remote.test/a")
    second = entry(uri="http://remote.test/b")
    third = entry(uri="http://remote.test/c")
    await small_cache.put(first)
    await small_cache.put(second)

    assert (
        await small_cache.get(first.uri) is not None
    ), "a hit makes the first entry the recent one"

    await small_cache.put(third)

    assert await small_cache.get(second.uri) is None, "the least recently used entry went"
    assert await small_cache.get(first.uri) is not None
    assert await small_cache.get(third.uri) is not None


async def test_get_refreshes_recency(small_cache):
    first = entry(uri="http://remote.test/a")
    second = entry(uri="http://remote.test/b")
    third = entry(uri="http://remote.test/c")
    await small_cache.put(first)
    await small_cache.put(second)

    assert await small_cache.get(second.uri) is not None, "the second entry is now the recent one"

    await small_cache.put(third)

    assert await small_cache.get(first.uri) is None
    assert await small_cache.get(second.uri) is not None


async def test_cap_is_respected_after_many_puts(small_cache):
    uris = [f"http://remote.test/note/{index}" for index in range(5)]

    for uri in uris:
        await small_cache.put(entry(uri=uri))

    stored = [uri for uri in uris if await small_cache.get(uri) is not None]
    assert stored == uris[-2:], "only the newest two survive"


async def test_an_older_cache_file_is_migrated(tmp_path):
    path = tmp_path / "cache.sqlite"
    connection = await aiosqlite.connect(path)
    await connection.execute("""
        CREATE TABLE entries (
            uri TEXT PRIMARY KEY,
            document TEXT,
            status_code INTEGER,
            fetched_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """)
    await connection.execute(
        "INSERT INTO entries VALUES (?, ?, ?, ?, ?)",
        ("http://remote.test/old", json.dumps(DOCUMENT), 200, 1_000.0, 1_300.0),
    )
    await connection.commit()
    await connection.close()

    cache = SqliteRemoteCache(path, max_entries=2)
    migrated = await cache.get("http://remote.test/old")
    assert migrated is not None and migrated.document == DOCUMENT, "the existing entry survived"

    await cache.put(entry(uri="http://remote.test/a"))
    await cache.put(entry(uri="http://remote.test/b"))

    assert await cache.get("http://remote.test/old") is None, "the old row is the least recent"
    await cache.close()
