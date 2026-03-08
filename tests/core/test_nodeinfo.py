from typing import Any, cast

import pytest

from firm.core.interfaces import Tenant
from firm.core.services.nodeinfo import nodeinfo_index, nodeinfo_version
from firm.core.store.memory import MemoryResourceStore
from firm.core.util import get_version
from tests.support import StubHttpRequest


async def test_nodeinfo_index():
    request = StubHttpRequest("GET", "https://example.com/.well-known/nodeinfo")
    store = MemoryResourceStore()
    request.app.state.store = store
    response = await nodeinfo_index(request)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/jrd+json"


@pytest.fixture
def tenant(tmp_path):
    return Tenant(
        "http://tenant1.test",
        MemoryResourceStore(),
        MemoryResourceStore(),
        tmp_path,
    )


async def test_nodeinfo_version(tenant: Tenant):
    request = StubHttpRequest(
        "GET", "https://example.com/nodeinfo/2.1", path_params={"version": "2.0"}, tenant=tenant
    )
    response = await nodeinfo_version(request)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    data = cast(dict[str, Any], response.json)
    assert data["software"]["name"] == "firm"
    assert data["software"]["version"] == get_version("firm")


# TODO Add test for custom nodeinfo
async def test_custom_metadata(tenant: Tenant):
    await tenant.private_store.put(
        {
            "type": "firm:NodeInfo",
            "attributedTo": tenant.prefix,
            "metadata": {"custom": "This is a custom nodeinfo metadata"},
        }
    )
    request = StubHttpRequest(
        "GET", "https://example.com/nodeinfo/2.0", path_params={"version": "2.0"}, tenant=tenant
    )
    response = await nodeinfo_version(request)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    data = cast(dict[str, Any], response.json)
    assert data["metadata"]["custom"] == "This is a custom nodeinfo metadata"
