import asyncio
import json

import pytest

from firm.core.interfaces import HttpResponse
from firm.core.remote import provider as provider_module
from firm.core.remote.cache import MemoryRemoteCache
from firm.core.remote.provider import (
    ACTIVITYPUB_ACCEPT,
    FetchPolicy,
    RemoteResourceProvider,
)
from tests.support import Clock, StubTransport, json_response

NOTE_URI = "http://remote.test/note/1"
PUBLIC_ADDRESS = "93.184.216.34"
NOTE = {
    "@context": "https://www.w3.org/ns/activitystreams",
    "id": NOTE_URI,
    "type": "Note",
    "content": "hello",
}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every hostname resolves to a public address unless a test says otherwise."""
    monkeypatch.setattr(
        provider_module, "_resolve_host", lambda host, port: [PUBLIC_ADDRESS], raising=False
    )


def _provider(
    transport: StubTransport,
    cache: MemoryRemoteCache | None = None,
    clock: Clock | None = None,
    policy: FetchPolicy | None = None,
) -> RemoteResourceProvider:
    return RemoteResourceProvider(
        cache or MemoryRemoteCache(),
        transport,
        policy=policy or FetchPolicy(),
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


async def test_document_id_mismatch_is_rejected():
    impostor = {**NOTE, "id": "http://remote.test/note/2"}
    transport = StubTransport({NOTE_URI: json_response(impostor)})
    cache = MemoryRemoteCache()
    provider = _provider(transport, cache)

    assert await provider.get(NOTE_URI) is None

    entry = await cache.get(NOTE_URI)
    assert entry is not None and entry.is_negative, "a document naming another URI is not content"
    assert entry.expires_at == entry.fetched_at + FetchPolicy().failure_ttl


async def test_non_object_body_is_rejected():
    transport = StubTransport(
        {NOTE_URI: HttpResponse(status_code=200, body=json.dumps([NOTE]).encode())}
    )
    cache = MemoryRemoteCache()
    provider = _provider(transport, cache)

    assert await provider.get(NOTE_URI) is None

    entry = await cache.get(NOTE_URI)
    assert entry is not None and entry.is_negative, "a JSON array is not an ActivityPub document"


async def test_oversized_body_is_rejected():
    policy = FetchPolicy(max_bytes=1024)
    body = json.dumps({**NOTE, "content": "x" * 2048}).encode()
    assert len(body) > policy.max_bytes, "the fixture must exceed the limit"

    transport = StubTransport({NOTE_URI: HttpResponse(status_code=200, body=body)})
    cache = MemoryRemoteCache()
    provider = _provider(transport, cache, policy=policy)

    assert await provider.get(NOTE_URI) is None

    entry = await cache.get(NOTE_URI)
    assert entry is not None and entry.is_negative, "an oversize body is refused, not truncated"
    assert entry.expires_at == entry.fetched_at + policy.failure_ttl


async def test_redirect_is_followed():
    canonical = "http://remote.test/note/1-canonical"
    transport = StubTransport(
        {
            NOTE_URI: HttpResponse(status_code=302, headers={"Location": canonical}),
            canonical: json_response({**NOTE, "id": canonical}),
        }
    )
    cache = MemoryRemoteCache()
    provider = _provider(transport, cache)

    document = await provider.get(NOTE_URI)

    assert document is not None and document["id"] == canonical
    assert transport.urls == [NOTE_URI, canonical], "one hop, in order"
    entry = await cache.get(NOTE_URI)
    assert (
        entry is not None and entry.document == document
    ), "cached under the URI that was asked for"


async def test_too_many_redirects_fail():
    hop = [NOTE_URI, *(f"http://remote.test/hop/{n}" for n in range(2, 6))]
    transport = StubTransport(
        {
            source: HttpResponse(status_code=302, headers={"Location": target})
            for source, target in zip(hop, hop[1:])
        }
    )
    cache = MemoryRemoteCache()
    provider = _provider(transport, cache)

    assert await provider.get(NOTE_URI) is None

    assert transport.urls == hop[:4], "the chain stops, it does not run forever"
    entry = await cache.get(NOTE_URI)
    assert entry is not None and entry.is_negative


async def test_non_http_scheme_is_refused():
    transport = StubTransport({})

    assert await _provider(transport).get("file:///etc/passwd") is None

    assert transport.urls == [], "refused before any connection"


async def test_credentials_in_uri_are_refused():
    transport = StubTransport({})

    assert await _provider(transport).get("http://user:pass@remote.test/note/1") is None

    assert transport.urls == []


async def test_loopback_hostname_is_refused(monkeypatch):
    def explode(host: str, port: int) -> list[str]:
        raise AssertionError("a loopback name must not even be resolved")

    monkeypatch.setattr(provider_module, "_resolve_host", explode)
    transport = StubTransport({})

    assert await _provider(transport).get("http://localhost/note/1") is None

    assert transport.urls == []


async def test_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(provider_module, "_resolve_host", lambda host, port: ["10.0.0.7"])
    transport = StubTransport({NOTE_URI: json_response(NOTE)})
    cache = MemoryRemoteCache()

    assert await _provider(transport, cache).get(NOTE_URI) is None

    assert transport.urls == [], "a host that resolves inward is never contacted"
    entry = await cache.get(NOTE_URI)
    assert entry is not None and entry.is_negative


async def test_redirect_to_private_address_is_refused(monkeypatch):
    def resolve(host: str, port: int) -> list[str]:
        return ["192.168.1.5"] if host == "internal.test" else [PUBLIC_ADDRESS]

    monkeypatch.setattr(provider_module, "_resolve_host", resolve)
    internal = "http://internal.test/secret"
    transport = StubTransport(
        {
            NOTE_URI: HttpResponse(status_code=302, headers={"Location": internal}),
            internal: json_response({"id": internal, "type": "Note"}),
        }
    )

    assert await _provider(transport).get(NOTE_URI) is None

    assert transport.urls == [NOTE_URI], "the public hop is fetched, the private one is not"


async def test_document_without_an_id_is_rejected():
    anonymous = {key: value for key, value in NOTE.items() if key != "id"}
    transport = StubTransport({NOTE_URI: json_response(anonymous)})
    cache = MemoryRemoteCache()

    assert await _provider(transport, cache).get(NOTE_URI) is None

    entry = await cache.get(NOTE_URI)
    assert entry is not None and entry.is_negative, "only a document that names itself is content"
    assert entry.expires_at == entry.fetched_at + FetchPolicy().failure_ttl


async def test_concurrent_fetches_are_deduped():
    transport = StubTransport({NOTE_URI: json_response(NOTE)}, delay=0.01)
    provider = _provider(transport)

    results = await asyncio.gather(*(provider.get(NOTE_URI) for _ in range(3)))

    assert results == [NOTE, NOTE, NOTE]
    assert transport.urls == [NOTE_URI], "three callers, one request"


async def test_stale_copy_is_served_when_refetch_fails():
    transport = StubTransport({NOTE_URI: json_response(NOTE)})
    clock = Clock()
    provider = _provider(transport, clock=clock)

    assert await provider.get(NOTE_URI) == NOTE
    transport.script(NOTE_URI, HttpResponse(status_code=500))
    clock.advance(FetchPolicy().ttl + 1)

    assert await provider.get(NOTE_URI) == NOTE, "a transient failure is not a deletion"
    assert len(transport.urls) == 2, "the refetch was attempted"


async def test_fragment_is_not_part_of_the_cache_key():
    transport = StubTransport({NOTE_URI: json_response(NOTE)})
    cache = MemoryRemoteCache()
    provider = _provider(transport, cache)

    assert await provider.get(f"{NOTE_URI}#frag") == NOTE

    assert transport.urls == [NOTE_URI], "the fragment never reaches the origin"
    assert await cache.get(NOTE_URI) is not None


async def test_invalidate_drops_the_cached_copy():
    transport = StubTransport({NOTE_URI: json_response(NOTE)})
    provider = _provider(transport)

    await provider.get(NOTE_URI)
    await provider.invalidate(NOTE_URI)
    await provider.get(NOTE_URI)

    assert transport.urls == [NOTE_URI, NOTE_URI], "the copy is forgotten, the origin still has it"


async def test_purge_and_close_delegate_to_the_cache():
    transport = StubTransport({NOTE_URI: json_response(NOTE)})
    cache = MemoryRemoteCache()
    clock = Clock()
    provider = _provider(transport, cache, clock)

    await provider.get(NOTE_URI)
    clock.advance(FetchPolicy().ttl + 1)
    assert await provider.purge() == 1

    await provider.close()
    assert await cache.get(NOTE_URI) is None
