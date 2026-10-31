"""Collection items as flat files beside the store's partition.

A collection's item list is two files under ``<store_path>/_items/``: a log holding one item
URI per line, oldest first, and a sparse index of the log's byte offsets, one record per
block of lines. An append is one write at the end of each file; a page is one seek in each.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from hashlib import md5
from pathlib import Path
from typing import Protocol

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    MAX_COLLECTION_PAGE_SIZE,
    CollectionPage,
    JSONObject,
    UnknownCollectionError,
)
from firm.core.store.collection_items import decode_cursor, encode_cursor
from firm.core.store.naming import uri_hash

BLOCK = 128
RECORD = 8
MEMBERSHIP_CACHE = 8


class FileCollectionStore(Protocol):
    """What these functions need from a store: where it lives, and its documents."""

    store_path: Path

    async def get(self, uri: str) -> JSONObject | None: ...

    async def put(self, resource: JSONObject) -> None: ...


class CollectionPaths:
    """The two files that hold one collection's item list."""

    def __init__(self, store_path: Path, collection_uri: str) -> None:
        self.directory = Path(store_path) / "_items"
        digest = uri_hash(collection_uri)
        self.log = self.directory / f"{digest}.log"
        self.index = self.directory / f"{digest}.idx"

    @property
    def exists(self) -> bool:
        return self.log.exists()


def _block_offset(paths: CollectionPaths, block: int) -> int | None:
    """The byte offset recorded for `block`, or None when the index has no record."""
    try:
        with open(paths.index, "rb") as index:
            index.seek(block * RECORD)
            raw = index.read(RECORD)
    except FileNotFoundError:
        return None
    return int.from_bytes(raw, "big") if len(raw) == RECORD else None


def read_lines(paths: CollectionPaths, start_line: int, count: int) -> list[str]:
    """`count` lines from `start_line`, oldest first.

    A final line without its newline is a torn write and is dropped.
    """
    if count <= 0 or not paths.exists:
        return []
    block = start_line // BLOCK
    offset = _block_offset(paths, block)
    first = block * BLOCK if offset is not None else 0
    lines: list[str] = []
    with open(paths.log, "rb") as log:
        log.seek(offset or 0)
        line = first
        while len(lines) < count:
            raw = log.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            if line >= start_line:
                lines.append(raw[:-1].decode())
            line += 1
    return lines


def _all_lines(paths: CollectionPaths) -> list[str]:
    if not paths.exists:
        return []
    data = paths.log.read_bytes()
    if not data.endswith(b"\n"):
        end = data.rfind(b"\n")
        data = data[: end + 1] if end >= 0 else b""
    return [chunk.decode() for chunk in data.split(b"\n") if chunk]


def append_line(paths: CollectionPaths, uri: str, line: int) -> None:
    """Append `uri` as line `line` of the log, recording a new block's offset."""
    paths.directory.mkdir(parents=True, exist_ok=True)
    with open(paths.log, "ab") as log:
        offset = log.tell()
        log.write(uri.encode() + b"\n")
    if line % BLOCK == 0:
        with open(paths.index, "ab") as index:
            index.write(offset.to_bytes(RECORD, "big"))


def rewrite(paths: CollectionPaths, lines: list[str]) -> None:
    """Replace both files with `lines`, oldest first."""
    paths.directory.mkdir(parents=True, exist_ok=True)
    log_tmp = paths.log.with_name(paths.log.name + ".tmp")
    index_tmp = paths.index.with_name(paths.index.name + ".tmp")
    offset = 0
    with open(log_tmp, "wb") as log, open(index_tmp, "wb") as index:
        for line, uri in enumerate(lines):
            raw = uri.encode() + b"\n"
            if line % BLOCK == 0:
                index.write(offset.to_bytes(RECORD, "big"))
            log.write(raw)
            offset += len(raw)
    os.replace(log_tmp, paths.log)
    os.replace(index_tmp, paths.index)


def _digest(uri: str) -> bytes:
    return md5(uri.encode()).digest()


_members: OrderedDict[str, set[bytes]] = OrderedDict()


def clear_membership() -> None:
    """Forget every cached digest set."""
    _members.clear()


def members(paths: CollectionPaths) -> set[bytes]:
    """The digests of the collection's items, rebuilt from the log when not cached."""
    key = str(paths.log)
    cached = _members.get(key)
    if cached is not None:
        _members.move_to_end(key)
        return cached
    known = {_digest(uri) for uri in _all_lines(paths)}
    _members[key] = known
    while len(_members) > MEMBERSHIP_CACHE:
        _members.popitem(last=False)
    return known


def _window(total: int, start: int, limit: int) -> tuple[int, int]:
    """(lowest line, count) for `limit` newest-first items beginning at `start`."""
    low = max(0, total - start - limit)
    high = total - start
    return low, max(0, high - low)


async def _document(store: FileCollectionStore, collection_uri: str) -> JSONObject:
    document = await store.get(collection_uri)
    if not document:
        raise UnknownCollectionError(f"Unknown collection: {collection_uri}")
    return document


def _total(document: JSONObject) -> int:
    total = document.get("totalItems", 0)
    return total if isinstance(total, int) else 0


async def append_item(store: FileCollectionStore, collection_uri: str, item_uri: str) -> int:
    document = await _document(store, collection_uri)
    total = _total(document)
    paths = CollectionPaths(store.store_path, collection_uri)
    append_line(paths, item_uri, total)
    members(paths).add(_digest(item_uri))
    document["totalItems"] = total + 1
    await store.put(document)
    return total + 1


async def remove_item(store: FileCollectionStore, collection_uri: str, item_uri: str) -> int:
    """Drop every copy of `item_uri`, rewriting the collection's files."""
    document = await _document(store, collection_uri)
    paths = CollectionPaths(store.store_path, collection_uri)
    known = members(paths)
    if _digest(item_uri) not in known:
        return _total(document)
    remaining = [uri for uri in _all_lines(paths) if uri != item_uri]
    rewrite(paths, remaining)
    known.discard(_digest(item_uri))
    document["totalItems"] = len(remaining)
    await store.put(document)
    return len(remaining)


async def contains_item(store: FileCollectionStore, collection_uri: str, item_uri: str) -> bool:
    if not await store.get(collection_uri):
        return False
    return _digest(item_uri) in members(CollectionPaths(store.store_path, collection_uri))


async def count_items(store: FileCollectionStore, collection_uri: str) -> int:
    document = await store.get(collection_uri)
    return _total(document) if document else 0


async def page_items(
    store: FileCollectionStore,
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
    total = _total(document)
    paths = CollectionPaths(store.store_path, collection_uri)
    if cursor is None:
        low, count = _window(total, 0, limit)
    else:
        line, _item = decode_cursor(cursor)
        low = max(0, line - limit)
        count = max(0, line - low)
    lines = read_lines(paths, low, count)
    next_cursor = encode_cursor(low, lines[0]) if low > 0 and lines else None
    return CollectionPage(
        items=tuple(reversed(lines)),
        cursor=next_cursor,
        total=total,
        start_index=total - (low + count),
    )
