"""Contract tests for the collection methods on ResourceStore.

Every backend must pass this file unmodified. A new backend adds a parametrization to
`store`; it does not fork the assertions.

Assert through the methods, never through the stored document: a backend may keep the item
list outside it. The one exception is ``totalItems``, which serialization reads from the
document and therefore the store must keep current.
"""

import pytest

from firm.core.interfaces import (
    MAX_COLLECTION_PAGE_SIZE,
    CollectionPage,
    UnknownCollectionError,
)
from firm.core.store.file import FileResourceStore
from firm.core.store.memory import MemoryResourceStore

COLLECTION = "http://server.test/collection"


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryResourceStore()
    return FileResourceStore(tmp_path, "test")


async def _collection(store, *items, key="items"):
    document = {
        "id": COLLECTION,
        "type": "OrderedCollection",
        key: list(items),
        "totalItems": len(items),
    }
    await store.put(document)
    return document


async def _seeded(store, *items):
    await _collection(store)
    for item in items:
        await store.append_item(COLLECTION, item)


async def test_append_prepends_and_counts(store):
    await _collection(store)
    assert await store.append_item(COLLECTION, "a") == 1
    assert await store.append_item(COLLECTION, "b") == 2
    assert await store.append_item(COLLECTION, "c") == 3
    assert (await store.page_items(COLLECTION)).items == ("c", "b", "a")
    assert (await store.get(COLLECTION))["totalItems"] == 3, "the wire reads totalItems"


async def test_append_allows_duplicates(store):
    """AS2 does not require unique items; refusing them is the caller's policy."""
    await _seeded(store, "a")
    assert await store.append_item(COLLECTION, "a") == 2
    assert (await store.page_items(COLLECTION)).items == ("a", "a")


async def test_append_to_an_unknown_collection_raises(store):
    with pytest.raises(UnknownCollectionError):
        await store.append_item("http://server.test/missing", "a")


async def test_unknown_collection_error_is_a_value_error(store):
    """Callers that catch ValueError today keep working."""
    assert issubclass(UnknownCollectionError, ValueError)


async def test_page_covers_a_collection_stored_with_ordered_items(store):
    """Documents written by older code use orderedItems as their storage key."""
    await _collection(store, "a", key="orderedItems")
    await store.append_item(COLLECTION, "b")
    assert (await store.page_items(COLLECTION)).items == ("b", "a")
    assert await store.count_items(COLLECTION) == 2


async def test_remove_drops_the_item_and_the_count(store):
    await _collection(store, "b", "a")
    assert await store.remove_item(COLLECTION, "b") == 1
    assert (await store.page_items(COLLECTION)).items == ("a",)
    assert (await store.get(COLLECTION))["totalItems"] == 1


async def test_remove_of_an_absent_item_is_a_noop(store):
    await _collection(store, "a")
    assert await store.remove_item(COLLECTION, "zzz") == 1
    assert (await store.page_items(COLLECTION)).items == ("a",)


async def test_remove_from_an_unknown_collection_raises(store):
    with pytest.raises(UnknownCollectionError):
        await store.remove_item("http://server.test/missing", "a")


async def test_contains_item(store):
    await _collection(store, "a")
    assert await store.contains_item(COLLECTION, "a")
    assert not await store.contains_item(COLLECTION, "b")


async def test_contains_of_an_unknown_collection_is_false(store):
    assert not await store.contains_item("http://server.test/missing", "a")


async def test_count_items(store):
    await _collection(store, "a", "b")
    assert await store.count_items(COLLECTION) == 2


async def test_count_of_an_unknown_collection_is_zero(store):
    assert await store.count_items("http://server.test/missing") == 0


async def test_page_walks_newest_first(store):
    await _seeded(store, "a", "b", "c", "d", "e")
    page = await store.page_items(COLLECTION, limit=2)
    assert page.items == ("e", "d")
    assert page.start_index == 0
    assert page.cursor is not None, "a cursor is a token; its shape is the store's business"
    assert page.total == 5
    page = await store.page_items(COLLECTION, page.cursor, limit=2)
    assert page.items == ("c", "b")
    assert page.start_index == 2
    page = await store.page_items(COLLECTION, page.cursor, limit=2)
    assert page.items == ("a",)
    assert page.start_index == 4
    assert page.cursor is None
    assert page.total == 5


async def test_page_cursor_survives_an_append(store):
    await _seeded(store, "a", "b", "c", "d", "e", "f")
    first = await store.page_items(COLLECTION, limit=2)
    assert first.items == ("f", "e")
    await store.append_item(COLLECTION, "g")
    second = await store.page_items(COLLECTION, first.cursor, limit=2)
    assert second.items == ("d", "c"), "the appended item shifts the walk, not repeats it"


async def test_page_survives_a_deletion_behind_the_cursor(store):
    """An item already returned can be removed without disturbing the rest of the walk."""
    await _seeded(store, "a", "b", "c", "d", "e", "f")
    first = await store.page_items(COLLECTION, limit=2)
    assert first.items == ("f", "e")
    await store.remove_item(COLLECTION, "f")
    second = await store.page_items(COLLECTION, first.cursor, limit=2)
    assert second.items == ("d", "c")


async def test_page_limit_is_bounded(store):
    await _seeded(store, *[f"item-{i}" for i in range(MAX_COLLECTION_PAGE_SIZE + 5)])
    page = await store.page_items(COLLECTION, limit=10_000)
    assert len(page.items) == MAX_COLLECTION_PAGE_SIZE
    assert page.cursor is not None


async def test_page_limit_below_one_raises(store):
    await _collection(store)
    with pytest.raises(ValueError):
        await store.page_items(COLLECTION, limit=0)


async def test_page_of_an_unknown_collection_is_empty(store):
    page = await store.page_items("http://server.test/missing")
    assert page == CollectionPage()


async def test_malformed_cursor_raises(store):
    await _collection(store, "a")
    with pytest.raises(ValueError):
        await store.page_items(COLLECTION, "not-a-cursor")
