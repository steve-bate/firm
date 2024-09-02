import pytest

from firm.interfaces import HttpException
from firm.services.webfinger import webfinger
from firm.store.memory import MemoryResourceStore
from tests.support import StubHttpRequest


async def test_webfinger():
    request = StubHttpRequest(
        "GET",
        (
            "https://example.com/.well-known/webfinger?"
            "resource=https://example.com/users/foo"
        ),
    )
    store = MemoryResourceStore()
    await store.put(
        {
            "id": "https://example.com/users/foo",
            "type": "Person",
            "preferredUsername": "foo",
        }
    )
    request.app.state.store = store
    response = await webfinger(request)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/jrd+json"


@pytest.mark.parametrize(
    "predicates, identities",
    [
        (["alsoKnownAs"], "acct:foo@server.test"),
        pytest.param(
            ["alias"], ["acct:foo@server.test", "acct:bar@server.test"], id="aliases"
        ),
    ],
)
async def test_webfinger_aka(predicates, identities):
    request = StubHttpRequest(
        "GET", "https://example.com/.well-known/webfinger?resource=acct:foo@server.test"
    )
    store = MemoryResourceStore()
    if predicates:
        for p in predicates:
            await store.put(
                {
                    "id": "https://example.com/users/foo",
                    "type": "Person",
                    "preferredUsername": "foo",
                    p: identities,
                }
            )
    request.app.state.store = store
    response = await webfinger(request, predicates)
    assert response.status_code == 200


async def test_webfinger_not_found():
    request = StubHttpRequest(
        "GET", "https://example.com/.well-known/webfinger?resource=acct:foo@server.test"
    )
    store = MemoryResourceStore()
    request.app.state.store = store
    with pytest.raises(HttpException, match="Not Found"):
        await webfinger(request)


async def test_webfinger_bad_request():
    request = StubHttpRequest("GET", "https://example.com/.well-known/webfinger")
    store = MemoryResourceStore()
    request.app.state.store = store
    with pytest.raises(HttpException, match="Bad Request"):
        await webfinger(request)
