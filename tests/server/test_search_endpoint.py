"""GET /search: authenticated, visibility-filtered, windowed and switchable."""

import pytest
from starlette.testclient import TestClient

from firm.server.config import (
    MemoryStoreConfig,
    SearchConfig,
    ServerConfig,
    TenantConfig,
)
from firm.server.server import app_factory, clear_app

PREFIX = "http://patch.test"
PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


def _public(uri: str, text: str = "cats") -> dict:
    return {"id": uri, "type": "Note", "summary": text, "to": [PUBLIC]}


def _private(uri: str, text: str = "cats") -> dict:
    return {"id": uri, "type": "Note", "summary": text}


async def _seed(client: TestClient, *documents: dict) -> None:
    tenant = next(iter(client.app.state.tenants.values()))
    for document in documents:
        await tenant.public_store.put(document)


def test_a_search_needs_a_principal(client):
    response = client.get("/search", params={"q": "cats"})

    assert response.status_code == 401


async def test_search_returns_matching_documents_the_principal_may_see(client, actor):
    await _seed(
        client,
        _public(f"{PREFIX}/notes/1"),
        _private(f"{PREFIX}/notes/2"),
        _public(f"{PREFIX}/notes/3", text="dogs"),
    )

    response = client.get("/search", params={"q": "cats"})

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["orderedItems"]] == [f"{PREFIX}/notes/1"]
    assert body["totalItems"] == 2, "the total counts matches, including the unseen one"
    assert body["type"] == "OrderedCollection"


async def test_the_window_bounds_a_search(client, actor):
    await _seed(client, *(_public(f"{PREFIX}/notes/{n}") for n in range(1, 4)))

    first = client.get("/search", params={"q": "cats", "limit": 1}).json()
    second = client.get("/search", params={"q": "cats", "start": 1, "limit": 1}).json()

    assert first["totalItems"] == 3
    assert len(first["orderedItems"]) == 1
    assert [item["id"] for item in first["orderedItems"]] != [
        item["id"] for item in second["orderedItems"]
    ]


@pytest.mark.parametrize("params", [{"q": "cats", "limit": 0}, {"q": "cats", "start": "abc"}])
def test_a_malformed_window_is_a_bad_request(client, actor, params):
    response = client.get("/search", params=params)

    assert response.status_code == 400


def test_search_can_be_switched_off(tmp_path):
    config = ServerConfig(
        [TenantConfig(prefix=PREFIX)],
        MemoryStoreConfig(tmp_path),
        search=SearchConfig(enabled=False),
    )
    with TestClient(app_factory(config), base_url=PREFIX) as client:
        try:
            response = client.get("/search", params={"q": "cats"})
            tenant = next(iter(client.app.state.tenants.values()))
            assert response.status_code == 404
            assert "search" not in tenant.endpoints
        finally:
            clear_app()


def test_the_tenant_advertises_its_search_endpoint(client):
    tenant = next(iter(client.app.state.tenants.values()))

    assert tenant.endpoints["search"] == f"{PREFIX}/search"
