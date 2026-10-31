"""The flat-file collection item store, exercised directly.

The store's own methods keep using the document-backed implementation until the cutover, so
these tests call `file_collections` directly. Collections are seeded with an empty item list:
importing an existing array is the cutover's job.
"""

import pytest

from firm.core.interfaces import UnknownCollectionError
from firm.core.store import file_collections
from firm.core.store.file import FileResourceStore

COLLECTION = "http://server.test/collection"
OTHER = "http://server.test/other"


@pytest.fixture
def store(tmp_path):
    return FileResourceStore(tmp_path, "public")


@pytest.fixture(autouse=True)
def clean_membership():
    file_collections.clear_membership()
    yield
    file_collections.clear_membership()


async def _collection(store, uri: str = COLLECTION) -> None:
    await store.put({"id": uri, "type": "OrderedCollection", "items": [], "totalItems": 0})


async def _seeded(store, *items, uri: str = COLLECTION) -> None:
    await _collection(store, uri)
    for item in items:
        await file_collections.append_item(store, uri, item)


def _paths(store, uri: str = COLLECTION):
    return file_collections.CollectionPaths(store.store_path, uri)


async def test_append_writes_one_log_line(store):
    await _collection(store)
    await file_collections.append_item(store, COLLECTION, "a")

    paths = _paths(store)
    assert paths.log.read_text() == "a\n"
    assert paths.index.stat().st_size == 8, "one index record for the first block"


async def test_append_writes_an_index_record_each_block(store):
    await _collection(store)
    for n in range(file_collections.BLOCK + 1):
        await file_collections.append_item(store, COLLECTION, f"item-{n}")

    paths = _paths(store)
    assert paths.index.stat().st_size == 16, "two blocks started: lines 0 and 128"


async def test_append_writes_a_second_line_for_a_duplicate(store):
    await _seeded(store, "a")

    assert await file_collections.append_item(store, COLLECTION, "a") == 2
    assert _paths(store).log.read_text() == "a\na\n"


async def test_append_to_an_unknown_collection_raises(store):
    with pytest.raises(UnknownCollectionError):
        await file_collections.append_item(store, "http://server.test/missing", "a")


async def test_count_comes_from_the_document(store):
    await _seeded(store, "a", "b")

    assert await file_collections.count_items(store, COLLECTION) == 2
    assert await file_collections.count_items(store, "http://server.test/missing") == 0


async def test_contains_reads_the_log(store):
    await _seeded(store, "a")

    assert await file_collections.contains_item(store, COLLECTION, "a")
    assert not await file_collections.contains_item(store, COLLECTION, "b")
    assert not await file_collections.contains_item(store, "http://server.test/missing", "a")


async def test_contains_survives_the_membership_cache_being_cleared(store):
    await _seeded(store, "a")
    assert await file_collections.contains_item(store, COLLECTION, "a")

    file_collections.clear_membership()

    assert await file_collections.contains_item(store, COLLECTION, "a"), "rebuilt from the log"


async def test_page_returns_items_newest_first(store):
    await _seeded(store, "a", "b", "c")

    page = await file_collections.page_items(store, COLLECTION)

    assert page.items == ("c", "b", "a")
    assert page.total == 3
    assert page.start_index == 0
    assert page.cursor is None


async def test_page_walks_to_the_end(store):
    await _seeded(store, "a", "b", "c", "d", "e")

    first = await file_collections.page_items(store, COLLECTION, limit=2)
    second = await file_collections.page_items(store, COLLECTION, first.cursor, limit=2)
    third = await file_collections.page_items(store, COLLECTION, second.cursor, limit=2)
    past_end = await file_collections.page_items(
        store, COLLECTION, file_collections.encode_cursor(99, "nope"), limit=2
    )

    assert (first.items, first.start_index) == (("e", "d"), 0)
    assert (second.items, second.start_index) == (("c", "b"), 2)
    assert (third.items, third.start_index) == (("a",), 4)
    assert third.cursor is None
    assert past_end.items == ()


async def test_page_reads_one_window_regardless_of_collection_size(store, monkeypatch):
    await _seeded(store, *[f"item-{n}" for n in range(2000)])
    reads: list[tuple[int, int]] = []
    original = file_collections.read_lines

    def counting(paths, start_line, count):
        reads.append((start_line, count))
        return original(paths, start_line, count)

    monkeypatch.setattr(file_collections, "read_lines", counting)

    page = await file_collections.page_items(store, COLLECTION, limit=20)

    assert len(page.items) == 20
    assert reads == [(1980, 20)], "one window of one page, independent of the collection size"


async def test_cursor_survives_an_append(store):
    await _seeded(store, *["a", "b", "c", "d", "e", "f"])
    first = await file_collections.page_items(store, COLLECTION, limit=2)

    await file_collections.append_item(store, COLLECTION, "g")
    second = await file_collections.page_items(store, COLLECTION, first.cursor, limit=2)

    assert first.items == ("f", "e")
    assert second.items == ("d", "c"), "the appended item does not repeat the walk"


async def test_cursor_survives_a_deletion_behind_the_walk(store):
    await _seeded(store, *["a", "b", "c", "d", "e", "f"])
    first = await file_collections.page_items(store, COLLECTION, limit=2)

    await file_collections.remove_item(store, COLLECTION, "f")
    second = await file_collections.page_items(store, COLLECTION, first.cursor, limit=2)

    assert first.items == ("f", "e")
    assert second.items == ("d", "c")


async def test_a_deletion_ahead_of_the_walk_continues(store):
    await _seeded(store, *["a", "b", "c", "d", "e", "f"])
    first = await file_collections.page_items(store, COLLECTION, limit=2)

    await file_collections.remove_item(store, COLLECTION, "a")
    second = await file_collections.page_items(store, COLLECTION, first.cursor, limit=2)
    third = await file_collections.page_items(store, COLLECTION, second.cursor, limit=2)

    assert first.items == ("f", "e")
    seen = [*second.items, *third.items]
    assert "b" in seen, "the walk still reaches the now-oldest item"
    assert "a" not in seen, "the removed item is gone"
    assert len(seen) - len(set(seen)) <= 1, "at most one item is repeated"


async def test_deleting_an_item_rewrites_the_collection(store):
    await _seeded(store, "a", "b", "c")

    assert await file_collections.remove_item(store, COLLECTION, "b") == 2

    paths = _paths(store)
    assert paths.log.read_text() == "a\nc\n", "the log is oldest first"
    assert (await store.get(COLLECTION))["totalItems"] == 2
    assert not await file_collections.contains_item(store, COLLECTION, "b")


async def test_deleting_an_absent_item_touches_nothing(store):
    await _seeded(store, "a")

    assert await file_collections.remove_item(store, COLLECTION, "zzz") == 1

    assert _paths(store).log.read_text() == "a\n"


async def test_a_torn_final_line_is_ignored(store):
    await _seeded(store, "a", "b")

    paths = _paths(store)
    paths.log.write_bytes(paths.log.read_bytes()[:-1])  # drop the last newline

    assert await file_collections.count_items(store, COLLECTION) == 2, "the document is the count"
    page = await file_collections.page_items(store, COLLECTION)
    assert page.items == ("a",), "the partial line is not an item"


async def test_two_collections_do_not_share_files(store):
    await _seeded(store, "a", uri=COLLECTION)
    await _seeded(store, "b", uri=OTHER)

    first = _paths(store, COLLECTION)
    second = _paths(store, OTHER)

    assert len({first.log, first.index, second.log, second.index}) == 4
    assert (await file_collections.page_items(store, COLLECTION)).items == ("a",)
    assert (await file_collections.page_items(store, OTHER)).items == ("b",)
