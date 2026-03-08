# Storage Routing

from urllib.parse import urlparse

import pytest

from firm.core.interfaces import JSONObject, ResourceStore
from firm.core.store.memory import MemoryResourceStore
from firm.core.store.prefixstore import PrefixAwareResourceStore


def test_private_uri():

    url = urlparse("private://firm.core.server.test")
    assert url.scheme == "private"
    assert url.netloc == "firm.core.server.test"


INSTANCE_PUBLIC_URI_1 = "https://instance1.test"
INSTANCE_PRIVATE_URI_1 = "private://instance1.test"

INSTANCE_PUBLIC_URI_2 = "https://instance2.test"
INSTANCE_PRIVATE_URI_2 = "private://instance2.test"


@pytest.fixture
def store():
    tenant_public_stores: dict[str, ResourceStore] = {
        INSTANCE_PUBLIC_URI_1: MemoryResourceStore(),
        INSTANCE_PUBLIC_URI_2: MemoryResourceStore(),
    }
    remote_store = MemoryResourceStore()
    private_store = MemoryResourceStore()
    tenant_private_stores: dict[str, ResourceStore] = {
        INSTANCE_PRIVATE_URI_1: private_store,
        INSTANCE_PRIVATE_URI_2: private_store,
    }
    return PrefixAwareResourceStore(tenant_public_stores, tenant_private_stores, remote_store)


async def test_url_prefix_routing_public(store):
    resource_1 = {"id": INSTANCE_PUBLIC_URI_1 + "/resource", "data": "test"}
    await store.tenant_public_stores[INSTANCE_PUBLIC_URI_1].put(resource_1)
    resource_2 = {"id": INSTANCE_PUBLIC_URI_2 + "/resource", "data": "test"}
    await store.tenant_public_stores[INSTANCE_PUBLIC_URI_2].put(resource_2)
    assert await store.get(INSTANCE_PUBLIC_URI_1 + "/resource") == resource_1
    assert await store.get(INSTANCE_PUBLIC_URI_2 + "/resource") == resource_2


async def test_url_prefix_routing(store):
    # setup tenant 1 resources
    public_resource_1 = {"id": INSTANCE_PUBLIC_URI_1 + "/resource", "data": "test"}
    await store.tenant_public_stores[INSTANCE_PUBLIC_URI_1].put(public_resource_1)
    private_resource_1 = {
        "id": INSTANCE_PRIVATE_URI_1 + "/confidential",
        "data": "test",
    }
    await store.tenant_private_stores[INSTANCE_PRIVATE_URI_1].put(private_resource_1)
    # setup tenant 2 resources
    public_resource_2 = {"id": INSTANCE_PUBLIC_URI_2 + "/resource", "data": "test"}
    await store.tenant_public_stores[INSTANCE_PUBLIC_URI_2].put(public_resource_2)
    private_resource_2 = {
        "id": INSTANCE_PRIVATE_URI_2 + "/confidential",
        "data": "test",
    }
    await store.tenant_private_stores[INSTANCE_PRIVATE_URI_2].put(private_resource_2)
    # setup remote resource
    remote_resource = {"id": "https://remote.test/resource", "data": "test"}
    # Test routing
    await store.remote_store.put(remote_resource)
    assert await store.get(INSTANCE_PUBLIC_URI_1 + "/resource") == public_resource_1
    assert await store.get(INSTANCE_PRIVATE_URI_1 + "/confidential") == private_resource_1
    assert await store.get(INSTANCE_PUBLIC_URI_2 + "/resource") == public_resource_2
    assert await store.get(INSTANCE_PRIVATE_URI_2 + "/confidential") == private_resource_2
    assert await store.get("https://remote.test/resource") == remote_resource


async def test_store() -> None:
    tenant_store1 = MemoryResourceStore()
    tenant_store2 = MemoryResourceStore()
    remote_store = MemoryResourceStore()
    store = PrefixAwareResourceStore(
        {
            "https://example1.test": tenant_store1,
            "https://example2.test": tenant_store2,
        },
        {
            "private://example1.test": MemoryResourceStore(),
            "private://example2.test": MemoryResourceStore(),
        },
        remote_store,
    )
    tenant_resource_uri_1 = "https://example1.test/r1"
    resource1: JSONObject = {"id": tenant_resource_uri_1}
    await store.put(resource1)
    tenant_resource_uri_2 = "https://example2.test/r2"
    resource2: JSONObject = {"id": tenant_resource_uri_2}
    await store.put(resource2)
    remote_resource_uri = "https://remote.test/foo"
    resource3: JSONObject = {"id": remote_resource_uri}
    await store.put(resource3)
    assert await store.get(tenant_resource_uri_1) == resource1
    assert await tenant_store1.get(tenant_resource_uri_1) == resource1
    assert await tenant_store1.get(remote_resource_uri) is None
    assert await tenant_store2.get(tenant_resource_uri_1) is None
    assert await store.get(tenant_resource_uri_2) == resource2
    assert await tenant_store2.get(tenant_resource_uri_2) == resource2
    assert await tenant_store2.get(remote_resource_uri) is None
    assert await tenant_store1.get(tenant_resource_uri_2) is None
    assert await remote_store.get(remote_resource_uri) == resource3
    assert await tenant_store1.get(remote_resource_uri) is None
    assert await tenant_store2.get(remote_resource_uri) is None
