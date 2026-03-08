import pytest

from firm.core.interfaces import HttpException, Tenant
from firm.core.services.webfinger import webfinger
from firm.core.store.memory import MemoryResourceStore
from tests.support import StubHttpRequest


@pytest.fixture
def tenant(tmp_path):
    return Tenant(
        "http://tenant1.test",
        MemoryResourceStore(),
        MemoryResourceStore(),
        tmp_path,
    )


async def test_webfinger(tenant: Tenant):
    request = StubHttpRequest(
        "GET",
        "https://example.com/.well-known/webfinger?resource=https://example.com/users/foo",
        tenant=tenant,
    )
    await tenant.public_store.put(
        {
            "id": "https://example.com/users/foo",
            "type": "Person",
            "preferredUsername": "foo",
        }
    )
    response = await webfinger(request)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/jrd+json"


@pytest.mark.parametrize(
    "predicates, identities",
    [
        (["alsoKnownAs"], "acct:foo@server.test"),
        pytest.param(["alias"], ["acct:foo@server.test", "acct:bar@server.test"], id="aliases"),
    ],
)
async def test_webfinger_aka(predicates, identities, tenant: Tenant):
    request = StubHttpRequest(
        "GET",
        "https://example.com/.well-known/webfinger?resource=acct:foo@server.test",
        tenant=tenant,
    )
    if predicates:
        for p in predicates:
            await tenant.public_store.put(
                {
                    "id": "https://example.com/users/foo",
                    "type": "Person",
                    "preferredUsername": "foo",
                    p: identities,
                }
            )
    response = await webfinger(request, predicates)
    assert response.status_code == 200


async def test_webfinger_not_found(tenant: Tenant):
    request = StubHttpRequest(
        "GET",
        "https://example.com/.well-known/webfinger?resource=acct:foo@server.test",
        tenant=tenant,
    )
    with pytest.raises(HttpException, match="Not Found"):
        await webfinger(request)


async def test_webfinger_bad_request():
    request = StubHttpRequest("GET", "https://example.com/.well-known/webfinger")
    store = MemoryResourceStore()
    request.app.state.store = store
    with pytest.raises(HttpException, match="Bad Request"):
        await webfinger(request)
