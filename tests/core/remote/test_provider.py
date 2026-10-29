from firm.core.remote.cache import MemoryRemoteCache
from firm.core.remote.provider import (
    ACTIVITYPUB_ACCEPT,
    FetchPolicy,
    RemoteResourceProvider,
)
from tests.support import StubTransport, json_response

NOTE_URI = "http://remote.test/note/1"
NOTE = {
    "@context": "https://www.w3.org/ns/activitystreams",
    "id": NOTE_URI,
    "type": "Note",
    "content": "hello",
}


def _provider(
    transport: StubTransport, cache: MemoryRemoteCache | None = None
) -> RemoteResourceProvider:
    return RemoteResourceProvider(cache or MemoryRemoteCache(), transport, policy=FetchPolicy())


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
