"""Filtered collection reads answered by scanning candidates a page at a time.

Every store can filter this way — it is the default ``ResourceStore.filter_items`` — and none
of them should if their layout can do better: a backend holding item metadata can answer with
matches directly and report a match count, and overrides this rather than falling back here.
The walk resolves candidates only through the caller's ``resolve``, so a store never fetches
and never decides authorization.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    JSON,
    MAX_COLLECTION_PAGE_SIZE,
    FilterPage,
    JSONObject,
    ResourceStore,
    decode_start,
    encode_start,
)
from firm.search.transform.jsonpath import JsonPathMatcher

CHUNK = MAX_COLLECTION_PAGE_SIZE
"""Candidates read per store call: the largest window ``page_items`` will serve."""


async def filter_items(
    store: ResourceStore,
    collection_uri: str,
    expression: str,
    cursor: str | None = None,
    *,
    limit: int = DEFAULT_COLLECTION_PAGE_SIZE,
    max_scan: int = MAX_COLLECTION_PAGE_SIZE,
    resolve: Callable[[str], Awaitable[JSONObject | None]] | None = None,
) -> FilterPage:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if max_scan < 1:
        raise ValueError("max_scan must be at least 1")
    if resolve is None:
        raise ValueError("filtering a collection needs a resolver for its items")
    matcher = JsonPathMatcher(expression)
    total = await store.count_items(collection_uri)
    position = 0
    if cursor is not None:
        decoded = decode_start(cursor)
        if decoded is None:
            raise ValueError(f"Malformed filter cursor: {cursor}")
        position = decoded
    start_index = position
    scanned = 0
    entries: list[JSON] = []
    while scanned < max_scan and len(entries) < limit and position < total:
        window = await store.page_items(
            collection_uri, start=position, limit=min(CHUNK, max_scan - scanned)
        )
        if not window.items:
            position = total
            break
        for item_uri in window.items:
            if scanned >= max_scan or len(entries) >= limit:
                break
            position += 1
            scanned += 1
            document = await resolve(item_uri)
            if document is None:
                continue
            # One item can project several values. They are kept together: a page never
            # splits a candidate's answer, so the cursor past it stays exact.
            entries.extend(matcher.values(document))
    return FilterPage(
        entries=tuple(entries),
        cursor=encode_start(position) if position < total else None,
        total=total,
        start_index=start_index,
        scanned=scanned,
    )
