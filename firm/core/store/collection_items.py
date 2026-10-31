"""Collection item lists, stored inside the collection document.

The item list of an ActivityPub collection is a JSON array of URIs, newest first. This module
implements the collection methods on ResourceStore for the stores that keep that array in the
collection document.
"""

from __future__ import annotations

from typing import Any, Protocol

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    MAX_COLLECTION_PAGE_SIZE,
    CollectionPage,
    JSONObject,
    UnknownCollectionError,
)


class CollectionDocumentStore(Protocol):
    """The two document operations these functions need from a store."""

    async def get(self, uri: str) -> JSONObject | None: ...

    async def put(self, resource: JSONObject) -> None: ...


# "items" is what this module writes; "orderedItems" is the key older documents use.
ITEMS_KEYS = ("items", "orderedItems")


def items_of(document: JSONObject) -> list[Any]:
    """The collection's item list, or an empty list when it holds none."""
    for key in ITEMS_KEYS:
        value = document.get(key)
        if isinstance(value, list):
            return value
    return []


def items_key_of(document: JSONObject) -> str:
    """The key the item list is written back to, preserving the document's own key."""
    if "items" not in document and "orderedItems" in document:
        return "orderedItems"
    return "items"


def encode_cursor(index: int, item: str) -> str:
    """A cursor naming position ``index`` in the stored item list."""
    return f"{index}:{item}"


def decode_cursor(cursor: str) -> tuple[int, str]:
    """``(index, item)`` from a cursor; the item is "" when it names no URI."""
    index, separator, item = cursor.partition(":")
    if not separator or not index.isdigit():
        raise ValueError(f"Malformed collection cursor: {cursor}")
    return int(index), item


async def _require(store: CollectionDocumentStore, collection_uri: str) -> JSONObject:
    document = await store.get(collection_uri)
    if not document:
        raise UnknownCollectionError(f"Unknown collection: {collection_uri}")
    return document


def _store_items(document: JSONObject, items: list[Any]) -> None:
    document[items_key_of(document)] = items
    document["totalItems"] = len(items)


async def append_item(store: CollectionDocumentStore, collection_uri: str, item_uri: str) -> int:
    document = await _require(store, collection_uri)
    items = items_of(document)
    items.insert(0, item_uri)
    _store_items(document, items)
    await store.put(document)
    return len(items)


async def remove_item(store: CollectionDocumentStore, collection_uri: str, item_uri: str) -> int:
    document = await _require(store, collection_uri)
    items = items_of(document)
    if item_uri not in items:
        return len(items)
    items.remove(item_uri)
    _store_items(document, items)
    await store.put(document)
    return len(items)


async def contains_item(store: CollectionDocumentStore, collection_uri: str, item_uri: str) -> bool:
    document = await store.get(collection_uri)
    return document is not None and item_uri in items_of(document)


async def count_items(store: CollectionDocumentStore, collection_uri: str) -> int:
    document = await store.get(collection_uri)
    return len(items_of(document)) if document else 0


async def page_items(
    store: CollectionDocumentStore,
    collection_uri: str,
    cursor: str | None = None,
    *,
    limit: int = DEFAULT_COLLECTION_PAGE_SIZE,
) -> CollectionPage:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    limit = min(limit, MAX_COLLECTION_PAGE_SIZE)
    document = await store.get(collection_uri)
    if not document:
        return CollectionPage()
    items = items_of(document)
    total = len(items)
    start = 0
    if cursor is not None:
        index, cursor_item = decode_cursor(cursor)
        if cursor_item and cursor_item in items:
            start = items.index(cursor_item) + 1
        else:
            # The cursor's item is gone; the slot it held now holds its successor.
            start = min(max(index, 0), total)
    window = items[start : start + limit]
    page = tuple(item for item in window if isinstance(item, str))
    next_cursor: str | None = None
    if window and start + len(window) < total:
        last = window[-1]
        next_cursor = encode_cursor(start + len(window) - 1, last if isinstance(last, str) else "")
    return CollectionPage(items=page, cursor=next_cursor, total=total, start_index=start)
