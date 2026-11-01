"""Collection item lists, stored inside the collection document.

A collection's item array is held under "items". For an OrderedCollection the array order is
the collection's order; for a Collection the order carries no meaning. This module implements
the collection methods on ResourceStore for the stores that keep that array in the document.
"""

from __future__ import annotations

from typing import Any, Protocol

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    MAX_COLLECTION_PAGE_SIZE,
    CollectionPage,
    JSONObject,
    UnknownCollectionError,
    decode_start,
    encode_start,
)
from firm.core.util import is_ordered_collection


class CollectionDocumentStore(Protocol):
    """The two document operations these functions need from a store."""

    async def get(self, uri: str) -> JSONObject | None: ...

    async def put(self, resource: JSONObject) -> None: ...


ITEMS = "items"


def items_of(document: JSONObject) -> list[Any]:
    """The collection's item list, or an empty list when it holds none."""
    items = document.get(ITEMS)
    return items if isinstance(items, list) else []


def insertion_index(count: int, offset: int) -> int:
    """The array index an ordered collection's `offset` names.

    0 is the front, -1 the end, and n is before the item currently at index n. This is the
    only definition of an offset in the codebase; every store calls it, and only for a
    collection whose order is meaningful.
    """
    if offset < -1:
        raise ValueError("offset must be 0, -1, or a non-negative index")
    if offset == -1:
        return count
    return min(offset, count)


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
    document[ITEMS] = items
    document["totalItems"] = len(items)


async def insert_item(
    store: CollectionDocumentStore,
    collection_uri: str,
    item_uri: str,
    *,
    offset: int = 0,
) -> int:
    document = await _require(store, collection_uri)
    items = items_of(document)
    if is_ordered_collection(document):
        index = insertion_index(len(items), offset)
    else:
        # A Collection has no order, so the offset is ignored and the item takes the
        # cheapest slot this storage offers: the end of the array.
        index = len(items)
    items.insert(index, item_uri)
    _store_items(document, items)
    await store.put(document)
    return len(items)


async def remove_item(store: CollectionDocumentStore, collection_uri: str, item_uri: str) -> int:
    """Remove every copy of item_uri; return the item count.

    AS2 puts no uniqueness rule on collection items, so a collection may hold the same URI
    more than once. Removal means the item is gone, which is what every caller assumes.
    """
    document = await _require(store, collection_uri)
    items = items_of(document)
    remaining = [item for item in items if item != item_uri]
    if len(remaining) == len(items):
        return len(items)
    _store_items(document, remaining)
    await store.put(document)
    return len(remaining)


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
    start: int | None = None,
    limit: int = DEFAULT_COLLECTION_PAGE_SIZE,
) -> CollectionPage:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    limit = min(limit, MAX_COLLECTION_PAGE_SIZE)
    document = await store.get(collection_uri)
    if not document:
        return CollectionPage()
    if cursor is not None and start is not None:
        raise ValueError("cursor and start are mutually exclusive")
    items = items_of(document)
    total = len(items)
    origin = 0
    if cursor is not None:
        token_start = decode_start(cursor)
        if token_start is not None:
            origin = min(token_start, total)
        else:
            index, cursor_item = decode_cursor(cursor)
            if cursor_item and cursor_item in items:
                origin = min(items.index(cursor_item) + 1, total)
            else:
                # The cursor's item is gone; the slot it held now holds its successor.
                origin = min(max(index, 0), total)
    elif start is not None:
        if start < 0:
            raise ValueError("start must not be negative")
        origin = min(start, total)
    window = items[origin : origin + limit]
    page = tuple(item for item in window if isinstance(item, str))
    next_cursor: str | None = None
    if window and origin + len(window) < total:
        last = window[-1]
        next_cursor = encode_cursor(origin + len(window) - 1, last if isinstance(last, str) else "")
    prev_cursor = encode_start(max(origin - limit, 0)) if origin > 0 else None
    return CollectionPage(
        items=page,
        cursor=next_cursor,
        prev_cursor=prev_cursor,
        total=total,
        start_index=origin,
    )
