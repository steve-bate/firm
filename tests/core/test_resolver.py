"""Routing: which local instance owns a URI, and the fetch path for the rest."""

from firm.core.interfaces import Tenant
from firm.core.remote.cache import CacheEntry, MemoryRemoteCache
from firm.core.remote.provider import FetchPolicy, RemoteResourceProvider
from firm.core.resolver import ResourceResolver
from firm.core.store.memory import MemoryResourceStore
from tests.support import CountingStore, StubTransport, json_response

A_PREFIX = "http://a.test"
B_PREFIX = "http://b.test"
NOTE_URI = "http://remote.test/note/1"
NOTE = {"id": NOTE_URI, "type": "Note"}
URN = "urn:uuid:3f6b1c8e-0f1a-4b2e-9c3d-2a7e5f8b9d01"


def _tenant(prefix: str, tmp_path) -> Tenant:
    return Tenant(prefix, CountingStore(), MemoryResourceStore(), tmp_path)


def _provider(transport: StubTransport) -> RemoteResourceProvider:
    return RemoteResourceProvider(
        MemoryRemoteCache(), transport, policy=FetchPolicy(allow_private_addresses=True)
    )


def test_owner_finds_the_tenant_by_prefix(tmp_path):
    a = _tenant(A_PREFIX, tmp_path)
    b = _tenant(B_PREFIX, tmp_path)

    resolver = ResourceResolver({A_PREFIX: a, B_PREFIX: b})

    assert resolver.owner(f"{B_PREFIX}/obj1") is b
    assert resolver.owner(f"{A_PREFIX}/obj1") is a


def test_owner_returns_the_longest_matching_prefix(tmp_path):
    short = _tenant("http://a.test", tmp_path)
    long = _tenant("http://a.test/two", tmp_path)

    resolver = ResourceResolver({"http://a.test": short, "http://a.test/two": long})

    assert resolver.owner("http://a.test/two/obj1") is long
    assert resolver.owner("http://a.test/obj1") is short


def test_owner_returns_none_for_an_unknown_prefix():
    resolver = ResourceResolver({})

    assert resolver.owner(NOTE_URI) is None


async def test_fetch_uses_the_provider():
    transport = StubTransport({NOTE_URI: json_response(NOTE)})
    resolver = ResourceResolver({}, _provider(transport))

    assert await resolver.fetch(NOTE_URI) == NOTE

    assert transport.urls == [NOTE_URI]


async def test_fetch_refuses_a_locally_owned_uri(tmp_path):
    transport = StubTransport({})
    resolver = ResourceResolver({A_PREFIX: _tenant(A_PREFIX, tmp_path)}, _provider(transport))

    assert await resolver.fetch(f"{A_PREFIX}/obj1") is None
    assert await resolver.fetch(URN) is None

    assert transport.urls == [], "our own documents are never fetched"


async def test_fetch_without_a_provider_returns_none():
    resolver = ResourceResolver({})

    assert await resolver.fetch(NOTE_URI) is None


async def test_purge_and_close_delegate_to_the_provider():
    cache = MemoryRemoteCache()
    provider = RemoteResourceProvider(
        cache, StubTransport({}), policy=FetchPolicy(allow_private_addresses=True)
    )
    await cache.put(
        CacheEntry(uri=NOTE_URI, document=None, status_code=404, fetched_at=0.0, expires_at=1.0)
    )
    resolver = ResourceResolver({}, provider)

    assert await resolver.purge() == 1
    await resolver.close()
    assert await cache.get(NOTE_URI) is None

    inert = ResourceResolver({})
    assert await inert.purge() == 0
    await inert.close()


async def test_fetch_returns_a_document_with_the_internal_item_key():
    collection = {
        "id": "http://remote.test/c",
        "type": "OrderedCollection",
        "orderedItems": ["a"],
    }
    transport = StubTransport({"http://remote.test/c": json_response(collection)})
    resolver = ResourceResolver({}, _provider(transport))

    fetched = await resolver.fetch("http://remote.test/c")

    assert fetched is not None
    assert fetched["items"] == ["a"]
    assert "orderedItems" not in fetched
