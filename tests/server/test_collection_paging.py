"""How a collection is served over HTTP: inline when small, a header or a page otherwise.

The store is whichever the test configures; these tests write through it, so they make no
assumption about where the item list lives.
"""

import pytest

from firm.core.interfaces import MAX_COLLECTION_PAGE_SIZE, Tenant

PREFIX = "http://patch.test"
ACTOR = f"{PREFIX}/actor1"
COLLECTION = f"{ACTOR}/outbox"


@pytest.fixture
def tenant(client) -> Tenant:
    return next(iter(client.app.state.tenants.values()))


@pytest.fixture(autouse=True)
async def authenticated(actor):
    """Every test here reads as the collection's owner: an outbox is not public."""
    return actor


async def _seed(tenant: Tenant, count: int) -> list[str]:
    """`count` stored notes, newest first in the collection, and the collection itself."""
    items = []
    for index in range(count):
        uri = f"{PREFIX}/note/{index:04d}"
        await tenant.public_store.put(
            {"id": uri, "type": "Note", "attributedTo": ACTOR, "content": f"note {index}"}
        )
        items.append(uri)
    await tenant.public_store.put(
        {
            "id": COLLECTION,
            "type": "OrderedCollection",
            "attributedTo": ACTOR,
            "items": items,
            "totalItems": count,
        }
    )
    return items


def _get(client, path: str):
    response = client.get(path, headers={"Accept": "application/activity+json"})
    assert response.is_success
    return response.json()


async def test_a_small_collection_is_still_inline(client, tenant):
    items = await _seed(tenant, 3)

    body = _get(client, COLLECTION)

    assert [item["id"] for item in body["orderedItems"]] == items
    assert body["totalItems"] == 3
    assert "first" not in body, "a collection that fits one page keeps its old shape"


async def test_a_large_collection_returns_a_header(client, tenant):
    await _seed(tenant, 30)

    body = _get(client, COLLECTION)

    assert body["totalItems"] == 30
    assert body["first"] == f"{COLLECTION}?page=true"
    assert body["last"] == f"{COLLECTION}?page=last"
    assert "orderedItems" not in body and "items" not in body


async def test_page_true_returns_the_first_page(client, tenant):
    items = await _seed(tenant, 30)

    body = _get(client, f"{COLLECTION}?page=true&limit=5")

    assert body["type"] == "OrderedCollectionPage"
    assert body["partOf"] == COLLECTION
    assert body["startIndex"] == 0
    assert body["totalItems"] == 30
    assert [item["id"] for item in body["orderedItems"]] == items[:5]
    assert body["next"]


async def test_numeric_page_matches_the_cursor_walk(client, tenant):
    items = await _seed(tenant, 30)

    first = _get(client, f"{COLLECTION}?page=true&limit=10")
    followed = _get(client, first["next"])
    numbered = _get(client, f"{COLLECTION}?page=2&limit=10")

    assert [item["id"] for item in followed["orderedItems"]] == items[10:20]
    assert [item["id"] for item in numbered["orderedItems"]] == items[10:20]
    assert numbered["startIndex"] == followed["startIndex"] == 10


async def test_last_page(client, tenant):
    items = await _seed(tenant, 30)

    body = _get(client, f"{COLLECTION}?page=last&limit=10")

    assert [item["id"] for item in body["orderedItems"]] == items[20:30]
    assert body["startIndex"] == 20
    assert "next" not in body, "the oldest page is the end of the walk"


async def test_a_cursor_page_is_self_consistent(client, tenant):
    items = await _seed(tenant, 30)
    first = _get(client, f"{COLLECTION}?page=true&limit=10")
    second_uri = first["next"]

    second = _get(client, second_uri)
    back = _get(client, second["prev"])

    assert second["id"] == second_uri
    assert second["startIndex"] == 10
    assert [item["id"] for item in back["orderedItems"]] == items[:10]
    assert back["startIndex"] == 0


async def test_a_page_beyond_the_end_is_empty(client, tenant):
    await _seed(tenant, 30)

    body = _get(client, f"{COLLECTION}?page=99&limit=10")

    assert body["type"] == "OrderedCollectionPage"
    assert "orderedItems" not in body, "serialization drops empty arrays"
    assert body["totalItems"] == 30
    assert "next" not in body


async def test_limit_is_clamped(client, tenant):
    await _seed(tenant, MAX_COLLECTION_PAGE_SIZE + 20)

    body = _get(client, f"{COLLECTION}?page=true&limit=10000")

    assert len(body["orderedItems"]) == MAX_COLLECTION_PAGE_SIZE


async def test_a_bad_limit_is_a_bad_request(client, tenant):
    await _seed(tenant, 3)

    response = client.get(f"{COLLECTION}?limit=0", headers={"Accept": "application/activity+json"})

    assert response.status_code == 400, "a malformed page size is the request's fault"


async def test_an_unknown_collection_pages_to_404(client, tenant):
    response = client.get(
        f"{ACTOR}/missing?page=true", headers={"Accept": "application/activity+json"}
    )

    assert response.status_code == 404


async def test_the_bounded_responses(client, tenant):
    """5,000 items: the header stays tiny and a page stays bounded, without timing it."""
    items = await _seed(tenant, 5000)

    header = client.get(COLLECTION, headers={"Accept": "application/activity+json"})
    first = client.get(f"{COLLECTION}?page=true", headers={"Accept": "application/activity+json"})
    last = client.get(f"{COLLECTION}?page=250", headers={"Accept": "application/activity+json"})

    assert header.is_success and first.is_success and last.is_success
    assert len(header.content) < 1024, "the header is a few hundred bytes"
    assert not any(uri.encode() in header.content for uri in items), "no item URIs in it"
    assert len(first.json()["orderedItems"]) == 20, "one page's worth"
    assert [item["id"] for item in last.json()["orderedItems"]] == items[-20:]
    assert len(last.content) < 100_000
