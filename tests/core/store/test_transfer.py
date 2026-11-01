"""Copying a store into another store: documents first, then collection items."""

import pytest

from firm.core.store.file import FileResourceStore
from firm.core.store.sqlite import SqliteResourceStore
from firm.core.store.transfer import DestinationNotEmpty, copy_store

NOTE = "https://server.test/note/1"
PERSON = "https://server.test/user1"
COLLECTION = "https://server.test/user1/outbox"


@pytest.fixture
async def stores(tmp_path):
    """A filesystem source and a sqlite destination, as the conversion pairs them."""
    source = FileResourceStore(tmp_path / "source" / "public")
    destination_directory = tmp_path / "destination"
    destination_directory.mkdir()
    destination = SqliteResourceStore("public", str(destination_directory / "public.sqlite"))
    yield source, destination
    await source.close()
    await destination.close()


async def test_it_copies_every_document(stores):
    source, destination = stores
    await source.put({"id": NOTE, "type": "Note", "content": "hello"})
    await source.put({"id": PERSON, "type": "Person", "name": "Steve"})

    report = await copy_store(source, destination)

    assert report.documents == 2
    note = await destination.get(NOTE)
    person = await destination.get(PERSON)
    assert note is not None and note["content"] == "hello"
    assert person is not None and person["name"] == "Steve"


async def test_it_copies_collection_items_in_order(stores):
    source, destination = stores
    await source.put(
        {
            "id": COLLECTION,
            "type": "OrderedCollection",
            "items": ["c", "b", "a"],
            "totalItems": 3,
        }
    )

    report = await copy_store(source, destination)

    page = await destination.page_items(COLLECTION, limit=10)
    assert page.items == ("c", "b", "a"), "newest first, as the source served it"
    assert page.total == 3
    assert report.collections == 1
    assert report.items == 3


async def test_the_copied_collection_document_holds_no_items(stores):
    source, destination = stores
    await source.put(
        {
            "id": COLLECTION,
            "type": "OrderedCollection",
            "items": ["b", "a"],
            "totalItems": 2,
        }
    )

    await copy_store(source, destination)

    stored = await destination.get(COLLECTION)
    assert stored is not None
    assert "items" not in stored and "orderedItems" not in stored
    assert stored["totalItems"] == 2, "the count of what was written, not of what was read"


async def test_it_copies_a_collection_that_is_already_migrated(stores):
    source, destination = stores
    await source.put({"id": COLLECTION, "type": "OrderedCollection", "totalItems": 0})
    await source.insert_item(COLLECTION, "a")
    await source.insert_item(COLLECTION, "b")

    await copy_store(source, destination)

    page = await destination.page_items(COLLECTION, limit=10)
    assert page.items == ("b", "a")
    assert page.total == 2


async def test_an_empty_collection_arrives_as_an_empty_collection(stores):
    source, destination = stores
    await source.put({"id": COLLECTION, "type": "OrderedCollection", "totalItems": 0})

    report = await copy_store(source, destination)

    assert report.collections == 1 and report.items == 0
    assert await destination.get(COLLECTION) is not None, "the document still exists"
    assert await destination.count_items(COLLECTION) == 0


async def test_a_second_copy_is_refused(stores):
    source, destination = stores
    await source.put({"id": NOTE, "type": "Note", "content": "hello"})
    await copy_store(source, destination)

    with pytest.raises(DestinationNotEmpty):
        await copy_store(source, destination)


async def test_an_empty_destination_is_copied_into(stores):
    source, destination = stores
    await source.put({"id": NOTE, "type": "Note", "content": "hello"})

    report = await copy_store(source, destination)

    assert report.documents == 1


async def test_force_replaces_what_the_destination_holds(stores):
    source, destination = stores
    await source.put({"id": NOTE, "type": "Note", "content": "first"})
    await source.put(
        {
            "id": COLLECTION,
            "type": "OrderedCollection",
            "items": ["b", "a"],
            "totalItems": 2,
        }
    )
    await copy_store(source, destination)

    # The source moves on, then the copy is repeated over the older destination.
    await source.put({"id": NOTE, "type": "Note", "content": "second"})
    await source.insert_item(COLLECTION, "c")

    await copy_store(source, destination, force=True)

    note = await destination.get(NOTE)
    assert note is not None and note["content"] == "second"
    page = await destination.page_items(COLLECTION, limit=10)
    assert page.items == ("c", "b", "a"), "one copy of each item, newest first"
    assert page.total == 3
