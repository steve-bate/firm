from typing import Any, cast

from firm.services.nodeinfo import nodeinfo_index, nodeinfo_version
from firm.store.memory import MemoryResourceStore
from firm.util import get_version
from tests.support import StubHttpRequest


async def test_nodeinfo_index():
    request = StubHttpRequest("GET", "https://example.com/.well-known/nodeinfo")
    store = MemoryResourceStore()
    request.app.state.store = store
    response = await nodeinfo_index(request)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/jrd+json"


async def test_nodeinfo_version():
    request = StubHttpRequest(
        "GET", "https://example.com/nodeinfo/2.1", path_params={"version": "2.0"}
    )
    store = MemoryResourceStore()
    request.app.state.store = store
    response = await nodeinfo_version(request)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    data = cast(dict[str, Any], response.json)
    assert data["software"]["name"] == "firm"
    assert data["software"]["version"] == get_version("firm")


# TODO Add test for custom nodeinfo
