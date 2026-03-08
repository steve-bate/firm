import pytest

from firm.core.interfaces import Tenant
from firm.core.services.webfinger import InvalidResourceUri, ResourceNotFound, webfinger
from firm.core.store.memory import MemoryResourceStore


@pytest.fixture
def tenant(tmp_path):
    return Tenant(
        "http://tenant1.test",
        MemoryResourceStore(),
        MemoryResourceStore(),
        tmp_path,
    )


async def test_webfinger(tenant: Tenant):
    await tenant.public_store.put(
        {
            "id": "https://example.com/users/foo",
            "type": "Person",
            "preferredUsername": "foo",
        }
    )
    data = await webfinger(tenant, "https://example.com/users/foo")
    assert data["subject"] == "https://example.com/users/foo"


@pytest.mark.parametrize(
    "predicates, identities",
    [
        (["alsoKnownAs"], "acct:foo@server.test"),
        pytest.param(["alias"], ["acct:foo@server.test", "acct:bar@server.test"], id="aliases"),
    ],
)
async def test_webfinger_aka(predicates, identities, tenant: Tenant):
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
    data = await webfinger(tenant, "https://example.com/users/foo", predicates)
    assert data["subject"] == "https://example.com/users/foo"


async def test_webfinger_not_found(tenant: Tenant):
    with pytest.raises(ResourceNotFound):
        await webfinger(tenant, "acct:foo@server.test")


async def test_webfinger_bad_request(tenant: Tenant):
    with pytest.raises(InvalidResourceUri):
        await webfinger(tenant, "invalid-resource-uri")
