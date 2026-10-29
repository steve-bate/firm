"""Cache entries and the implementations that persist them."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import aiosqlite

from firm.core.interfaces import JSONObject


@dataclass(frozen=True)
class CacheEntry:
    """One answer about one URI: a document, or a remembered failure."""

    uri: str
    document: JSONObject | None
    status_code: int | None
    fetched_at: float
    expires_at: float

    def is_fresh(self, now: float) -> bool:
        """Whether this entry may be served without asking the origin again."""
        return now < self.expires_at

    @property
    def is_negative(self) -> bool:
        """A remembered failure rather than a document."""
        return self.document is None


@runtime_checkable
class RemoteCache(Protocol):
    """Where entries live. Keys are cache keys, not raw URIs."""

    async def get(self, key: str) -> CacheEntry | None: ...

    async def put(self, entry: CacheEntry) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def purge_expired(self, now: float) -> int: ...

    async def close(self) -> None: ...


class MemoryRemoteCache:
    """An in-process cache, unbounded by design."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    async def get(self, key: str) -> CacheEntry | None:
        return self._entries.get(key)

    async def put(self, entry: CacheEntry) -> None:
        self._entries[entry.uri] = entry

    async def delete(self, key: str) -> None:
        self._entries.pop(key, None)

    async def purge_expired(self, now: float) -> int:
        expired = [key for key, entry in self._entries.items() if not entry.is_fresh(now)]
        for key in expired:
            del self._entries[key]
        return len(expired)

    async def close(self) -> None:
        self._entries.clear()


class SqliteRemoteCache:
    """A cache in one SQLite file: a row per URI, purged with one statement."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._connection: aiosqlite.Connection | None = None

    async def _db(self) -> aiosqlite.Connection:
        if self._connection is not None:
            return self._connection
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path)
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                uri TEXT PRIMARY KEY,
                document TEXT,
                status_code INTEGER,
                fetched_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """)
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS entries_expires_at ON entries (expires_at)"
        )
        await connection.commit()
        self._connection = connection
        return connection

    async def get(self, key: str) -> CacheEntry | None:
        db = await self._db()
        async with db.execute(
            "SELECT document, status_code, fetched_at, expires_at FROM entries WHERE uri = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        document, status_code, fetched_at, expires_at = row
        return CacheEntry(
            uri=key,
            document=json.loads(document) if document is not None else None,
            status_code=status_code,
            fetched_at=fetched_at,
            expires_at=expires_at,
        )

    async def put(self, entry: CacheEntry) -> None:
        db = await self._db()
        await db.execute(
            """
            INSERT INTO entries (uri, document, status_code, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(uri) DO UPDATE SET
                document = excluded.document,
                status_code = excluded.status_code,
                fetched_at = excluded.fetched_at,
                expires_at = excluded.expires_at
            """,
            (
                entry.uri,
                json.dumps(entry.document) if entry.document is not None else None,
                entry.status_code,
                entry.fetched_at,
                entry.expires_at,
            ),
        )
        await db.commit()

    async def delete(self, key: str) -> None:
        db = await self._db()
        await db.execute("DELETE FROM entries WHERE uri = ?", (key,))
        await db.commit()

    async def purge_expired(self, now: float) -> int:
        db = await self._db()
        cursor = await db.execute("DELETE FROM entries WHERE expires_at <= ?", (now,))
        await db.commit()
        return cursor.rowcount

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
