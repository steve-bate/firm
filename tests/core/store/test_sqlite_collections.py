"""What the SQLite collection backend does beyond the five-method contract.

The contract itself is `tests/core/store/test_collection_items.py`, which runs against this
store through its `sqlite` parameter. This file pins only what is specific to the table:
positions, gaps, the import from a legacy array, and the document's `totalItems`.
"""

import json

import pytest

from firm.core.store.sqlite import SqliteResourceStore

COLLECTION = "http://server.test/collection"


@pytest.fixture
async def store(tmp_path):
    store = SqliteResourceStore("test", str(tmp_path / "collections.sqlite"))
    yield store
    await store.close()


async def _collection(store, **document):
    await store.put({"id": COLLECTION, "type": "OrderedCollection", **document})


async def _seeded(store, *items):
    await _collection(store)
    for item in items:
        await store.insert_item(COLLECTION, item)


def _rows(store):
    return store.connection.execute(
        "SELECT position, uri FROM collection_items WHERE collection = ? ORDER BY position",
        (COLLECTION,),
    ).fetchall()


def _document(store):
    (object_json,) = store.connection.execute(
        "SELECT object FROM objects WHERE partition = ? and uri = ?", ("test", COLLECTION)
    ).fetchone()
    return json.loads(object_json)


async def test_append_assigns_increasing_positions(store):
    await _seeded(store, "a", "b", "c")

    assert _rows(store) == [(1, "a"), (2, "b"), (3, "c")], "the newest item has the highest"
    page = await store.page_items(COLLECTION)
    assert page.items == ("c", "b", "a")


async def test_append_writes_a_second_row_for_a_duplicate(store):
    await _seeded(store, "a")

    count = await store.insert_item(COLLECTION, "a")

    assert count == 2
    assert _rows(store) == [(1, "a"), (2, "a")], "AS2 does not require unique items"


async def test_delete_does_not_renumber(store):
    await _seeded(store, "a", "b", "c")

    await store.remove_item(COLLECTION, "b")

    assert _rows(store) == [(1, "a"), (3, "c")], "positions are never reused or renumbered"


async def test_a_page_walk_survives_a_delete_ahead_of_the_cursor(store):
    """Unlike the flat-file backend, gaps cost a walk nothing: a cursor is a position."""
    await _seeded(store, "a", "b", "c", "d", "e", "f")
    first = await store.page_items(COLLECTION, limit=2)

    await store.remove_item(COLLECTION, "a")
    second = await store.page_items(COLLECTION, first.cursor, limit=2)

    assert first.items == ("f", "e")
    assert second.items == ("d", "c"), "exact: the removed item was never in the window"


async def test_start_index_counts_live_items_above_the_page(store):
    await _seeded(store, "a", "b", "c", "d", "e", "f")
    for item in ("b", "c", "d", "e"):
        await store.remove_item(COLLECTION, item)
    assert _rows(store) == [(1, "a"), (6, "f")], "a gap of four"

    first = await store.page_items(COLLECTION, limit=1)
    second = await store.page_items(COLLECTION, first.cursor, limit=1)

    assert (first.items, first.start_index) == (("f",), 0)
    assert (second.items, second.start_index) == (("a",), 1), "one live item above, not five"


async def test_the_document_total_matches_the_table(store):
    await _seeded(store, "a", "b")

    await store.remove_item(COLLECTION, "a")

    (count,) = store.connection.execute(
        "SELECT COUNT(*) FROM collection_items WHERE collection = ?", (COLLECTION,)
    ).fetchone()
    assert count == 1
    assert _document(store)["totalItems"] == count


async def test_an_existing_document_is_imported_once(store):
    await _collection(store, items=["a", "b", "c"])

    page = await store.page_items(COLLECTION)

    assert page.items == ("a", "b", "c"), "a document stores its items newest first"
    assert page.total == 3
    assert _rows(store) == [(1, "c"), (2, "b"), (3, "a")], "the array's first item is the newest"
    assert "items" not in _document(store), "the key is gone once the rows exist"
    assert _document(store)["totalItems"] == 3

    assert (await store.page_items(COLLECTION)).items == ("a", "b", "c")
    assert _rows(store) == [(1, "c"), (2, "b"), (3, "a")], "a second call imports nothing"


async def test_a_document_with_ordered_items_is_imported(store):
    await _collection(store, orderedItems=["x", "y"])

    page = await store.page_items(COLLECTION)

    assert page.items == ("x", "y"), "newest first, like the items key"
    assert _rows(store) == [(1, "y"), (2, "x")]
    assert "orderedItems" not in _document(store)


async def test_the_table_wins_when_both_exist(store):
    await _collection(store, items=["a", "b"])
    await store.page_items(COLLECTION)  # imports a, b

    await _collection(store, items=["z"])  # a stale array alongside live rows

    assert (await store.page_items(COLLECTION)).items == ("a", "b"), "the rows are served"
    assert _rows(store) == [(1, "b"), (2, "a")], "nothing is re-imported or duplicated"
    assert "items" not in _document(store)


async def test_insert_at_the_end_serves_the_item_last(store):
    await _collection(store, items=["a"])
    await store.insert_item(COLLECTION, "b", offset=-1)
    page = await store.page_items(COLLECTION, limit=10)
    assert page.items == ("a", "b")


async def test_insert_at_an_index_places_the_item_before_it(store):
    await _collection(store, items=["a", "b", "c"])
    await store.insert_item(COLLECTION, "x", offset=1)
    page = await store.page_items(COLLECTION, limit=10)
    assert page.items == ("a", "x", "b", "c")


async def test_insert_rejects_an_offset_below_minus_one(store):
    await _collection(store, items=["a"])
    with pytest.raises(ValueError):
        await store.insert_item(COLLECTION, "b", offset=-2)


async def test_insert_ignores_the_offset_for_an_unordered_collection(store):
    await store.put({"id": COLLECTION, "type": "Collection", "items": ["a"]})
    await store.insert_item(COLLECTION, "b", offset=-1)
    page = await store.page_items(COLLECTION, limit=10)
    assert page.items == ("b", "a"), "a Collection has no order, so -1 does not mean the end"
