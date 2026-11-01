"""Copy one store's documents and collection items into another store.

The copy uses only the ``ResourceStore`` API, so any pair of backends works: the source is read
with ``query`` and ``page_items``, the destination written with ``put`` and ``insert_item``.
Collections arrive migrated: the item array stays behind, the items go into the destination's
collection storage, and the document's ``totalItems`` is left at zero for the appends to count
up, which is the line index the file store expects.
"""

from __future__ import annotations

from dataclasses import dataclass

from firm.core.interfaces import MAX_COLLECTION_PAGE_SIZE, JSONObject, ResourceStore
from firm.core.util import get_id, is_collection


class DestinationNotEmpty(Exception):
    """The destination already holds documents and `force` was not asked for."""


@dataclass(frozen=True)
class CopyReport:
    """What a copy wrote."""

    documents: int = 0
    collections: int = 0
    items: int = 0


async def copy_store(
    source: ResourceStore, destination: ResourceStore, *, force: bool = False
) -> CopyReport:
    """Copy every document and every collection item from `source` into `destination`.

    Without `force` the destination must hold no documents. With it, documents are overwritten
    and a destination collection that already holds items is emptied first, so one copy of each
    item ends up stored.
    """
    if not force and await destination.query({}):
        raise DestinationNotEmpty(
            "the destination already holds documents; remove it or pass force to overwrite"
        )
    documents: list[JSONObject] = await source.query({})
    collection_uris = _collection_uris(documents)
    if force:
        for collection_uri in collection_uris:
            if await destination.count_items(collection_uri):
                await _clear_items(destination, collection_uri)
    for document in documents:
        await destination.put(_for_destination(document))
    items = 0
    for collection_uri in collection_uris:
        oldest_first = await _items_oldest_first(source, collection_uri)
        for item_uri in oldest_first:
            await destination.insert_item(collection_uri, item_uri)
        items += len(oldest_first)
    return CopyReport(len(documents), len(collection_uris), items)


def _collection_uris(documents: list[JSONObject]) -> list[str]:
    """The ids of the collections among `documents`, in the order the store returned them."""
    uris = []
    for document in documents:
        if not is_collection(document):
            continue
        uri = get_id(document)
        if uri is None:
            raise ValueError("a collection document has no id")
        uris.append(uri)
    return uris


def _for_destination(document: JSONObject) -> JSONObject:
    """`document` as the destination stores it: items live in the collection storage."""
    if not is_collection(document):
        return document
    copied = {key: value for key, value in document.items() if key != "items"}
    copied["totalItems"] = 0
    return copied


async def _items_oldest_first(store: ResourceStore, collection_uri: str) -> list[str]:
    """Every item of a collection, oldest first — the order a log or a table expects."""
    page = await store.page_items(collection_uri, limit=MAX_COLLECTION_PAGE_SIZE)
    items = list(page.items)
    while page.cursor:
        page = await store.page_items(collection_uri, page.cursor, limit=MAX_COLLECTION_PAGE_SIZE)
        items.extend(page.items)
    items.reverse()
    return items


async def _clear_items(destination: ResourceStore, collection_uri: str) -> None:
    """Remove every item from a collection in the destination.

    Each pass removes every item of the current first page, which the store removes from its
    log or table, so the collection shrinks until it is empty.
    """
    while True:
        page = await destination.page_items(collection_uri, limit=MAX_COLLECTION_PAGE_SIZE)
        if not page.items:
            return
        for item_uri in page.items:
            await destination.remove_item(collection_uri, item_uri)
