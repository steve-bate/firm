from typing import cast

import pytest

from firm.core.interfaces import JSONObject, Tenant
from firm.core.services.nodeinfo import nodeinfo_index, nodeinfo_version
from firm.core.store.memory import MemoryResourceStore
from firm.core.util import get_version


async def test_nodeinfo_index():
    # request = StubHttpRequest("GET", "https://example.com/.well-known/nodeinfo")
    # store = MemoryResourceStore()
    # request.app.state.store = store
    data = await nodeinfo_index(request_url="https://example.com/.well-known/nodeinfo")
    assert data["links"][0]["rel"] == "http://nodeinfo.diaspora.software/ns/schema/2.0"


@pytest.fixture
def tenant(tmp_path):
    return Tenant(
        "http://tenant1.test",
        MemoryResourceStore(),
        MemoryResourceStore(),
        tmp_path,
    )


async def test_nodeinfo_version(tenant: Tenant):
    data = await nodeinfo_version(tenant, version="2.0")
    software = cast(JSONObject, data["software"])
    assert software["name"] == "firm"
    assert software["version"] == get_version("firm")


# TODO Add test for custom nodeinfo
async def test_custom_metadata(tenant: Tenant):
    await tenant.private_store.put(
        {
            "type": "firm:NodeInfo",
            "attributedTo": tenant.prefix,
            "metadata": {"custom": "This is a custom nodeinfo metadata"},
        }
    )
    data = await nodeinfo_version(tenant, version="2.0")
    metadata = cast(JSONObject, data["metadata"])
    assert str(metadata["custom"]) == "This is a custom nodeinfo metadata"
