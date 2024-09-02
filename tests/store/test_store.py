from firm.interfaces import JSONObject
from firm.store.memory import MemoryResourceStore
from firm.store.prefixstore import PrefixAwareResourceStore


async def test_store() -> None:
    tenant_store1 = MemoryResourceStore()
    tenant_store2 = MemoryResourceStore()
    remote_store = MemoryResourceStore()
    store = PrefixAwareResourceStore(
        {
            "https://example1.test": tenant_store1,
            "https://example2.test": tenant_store2,
        },
        remote_store,
        MemoryResourceStore(),
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
