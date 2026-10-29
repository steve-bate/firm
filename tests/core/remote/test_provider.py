from firm.core.interfaces import HttpResponse
from firm.core.remote.cache import MemoryRemoteCache
from firm.core.remote.provider import (
    ACTIVITYPUB_ACCEPT,
    FetchPolicy,
    RemoteResourceProvider,
)
from tests.support import Clock, StubTransport, json_response

NOTE_URI = "http://remote.test/note/1"
NOTE = {
    "@context": "https://www.w3.org/ns/activitystreams",
    "id": NOTE_URI,
    "type": "Note",
    "content": "hello",
}


def _provider(
    transport: StubTransport,
    cache: MemoryRemoteCache | None = None,
    clock: Clock | None = None,
) -> RemoteResourceProvider:
    return RemoteResourceProvider(
        cache or MemoryRemoteCache(),
        transport,
        policy=FetchPolicy(),
        clock=clock or Clock(),
    )


async def test_get_fetches_and_caches():
    transport = StubTransport({NOTE_URI: json_response(NOTE)})
    cache = MemoryRemoteCache()

    assert await _provider(transport, cache).get(NOTE_URI) == NOTE

    assert transport.urls == [NOTE_URI], "exactly one request"
    assert transport.requests[0][1]["Accept"] == ACTIVITYPUB_ACCEPT

    entry = await cache.get(NOTE_URI)
    assert entry is not None, "the fetched document is cached"
    assert entry.document == NOTE
    assert entry.expires_at > entry.fetched_at, "the entry is stored with a future expiry"


async def test_cache_hit_does_not_refetch():
    transport = StubTransport({NOTE_URI: json_response(NOTE)})
    provider = _provider(transport)

    await provider.get(NOTE_URI)
    assert await provider.get(NOTE_URI) == NOTE

    assert transport.urls == [NOTE_URI], "a second call inside the TTL makes no request"


async def test_expired_entry_is_refetched():
    transport = StubTransport({NOTE_URI: json_response(NOTE)})
    clock = Clock()
    provider = _provider(transport, clock=clock)

    await provider.get(NOTE_URI)
    clock.advance(FetchPolicy().ttl + 1)
    assert await provider.get(NOTE_URI) == NOTE

    assert transport.urls == [NOTE_URI, NOTE_URI], "one further request once the entry is stale"


async def test_failure_is_negatively_cached():
    transport = StubTransport({NOTE_URI: HttpResponse(status_code=500)})
    cache = MemoryRemoteCache()
    clock = Clock()
    provider = _provider(transport, cache, clock)

    assert await provider.get(NOTE_URI) is None
    assert await provider.get(NOTE_URI) is None

    assert transport.urls == [NOTE_URI], "the failure is remembered, so the origin is not hit twice"

    entry = await cache.get(NOTE_URI)
    assert entry is not None and entry.is_negative
    assert entry.expires_at == entry.fetched_at + FetchPolicy().failure_ttl


async def test_not_found_stays_cached_for_the_long_ttl():
    transport = StubTransport({NOTE_URI: HttpResponse(status_code=404)})
    cache = MemoryRemoteCache()
    clock = Clock()
    provider = _provider(transport, cache, clock)

    assert await provider.get(NOTE_URI) is None

    entry = await cache.get(NOTE_URI)
    assert entry is not None, "a 404 is an answer, not a miss"
    assert entry.is_negative and entry.status_code == 404
    assert entry.expires_at == entry.fetched_at + FetchPolicy().not_found_ttl

    clock.advance(FetchPolicy().failure_ttl * 2)
    assert await provider.get(NOTE_URI) is None
    assert transport.urls == [NOTE_URI], "well past failure_ttl, the 404 is still trusted"
