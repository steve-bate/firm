import json
import sqlite3
from contextlib import closing
from typing import Any, cast

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    MAX_COLLECTION_PAGE_SIZE,
    CollectionPage,
    JSONObject,
    QueryCriteria,
    UnknownCollectionError,
    decode_start,
    encode_start,
)
from firm.core.store.base import ResourceStoreBase

ITEMS_KEYS = ("items", "orderedItems")


class SqliteResourceStore(ResourceStoreBase):
    def __init__(self, name: str, db: str):
        self.name = name
        self.connection = sqlite3.connect(db)
        self._initialize_table()

    def commit(self) -> None:
        self.connection.commit()

    async def close(self) -> None:
        self.connection.close()

    def _initialize_table(self) -> None:
        with closing(self.connection.cursor()) as cursor:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS objects (
                partition TEXT NOT NULL,
                uri TEXT NOT NULL,
                object JSON NOT NULL,
                PRIMARY KEY (partition, uri)
            )
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS collection_items (
                collection TEXT    NOT NULL,
                position   INTEGER NOT NULL,
                uri        TEXT    NOT NULL,
                PRIMARY KEY (collection, position)
            )
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS ix_collection_items_uri
                ON collection_items (collection, uri)
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS ix_collection_items_order
                ON collection_items (collection, position DESC)
            """)

    async def get(self, uri: str) -> dict[str, Any] | None:
        """Retrieve Object based on uri"""
        with closing(self.connection.cursor()) as cursor:
            rows = cursor.execute(
                "SELECT object FROM objects WHERE partition = ? and uri = ? LIMIT 1",
                (self.name, uri),
            ).fetchone()
            if rows:
                return cast(dict[str, Any], json.loads(rows[0]))
            else:
                return None

    async def is_stored(self, uri: str) -> bool:
        with closing(self.connection.cursor()) as cursor:
            (count,) = cursor.execute(
                "SELECT count(*) FROM objects WHERE partition = ? and uri = ?",
                (self.name, uri),
            ).fetchone()
            return cast(int, count) > 0

    async def put(self, resource: JSONObject) -> None:
        """Store an AP Object"""
        uri = str(resource["id"])
        with self.connection:
            self.connection.execute(
                """INSERT INTO objects (partition, uri, object) VALUES (?,?,?)
                   ON CONFLICT(partition, uri) DO UPDATE SET object = excluded.object""",
                (self.name, uri, json.dumps(resource)),
            )

    async def remove(self, uri: str) -> None:
        """Remove an object from the store"""
        with self.connection:
            self.connection.execute(
                "DELETE FROM objects WHERE partition = ? and uri = ?",
                (self.name, uri),
            )

    # NOTE This API will change
    async def query(self, criteria: QueryCriteria) -> list[JSONObject]:
        conditions = []
        params: list[Any] = [self.name]
        for key, value in criteria.items():
            path = f"$.{key}"
            conditions.append(
                "(json_extract(object, ?) = ? or (json_type(object, ?) = 'array' and ? in "
                "(select value from json_each(object, ?))))"
            )
            params.extend([path, value, path, value, path])
        sql = "SELECT object FROM objects WHERE partition = ?"
        if conditions:
            sql += " and " + " and ".join(conditions)
        with closing(self.connection.cursor()) as cursor:
            rows = cursor.execute(sql, params).fetchmany(100)
            return [json.loads(row[0]) for row in rows] if rows else []

    def _item_count(self, collection_uri: str) -> int:
        (count,) = self.connection.execute(
            "SELECT COUNT(*) FROM collection_items WHERE collection = ?", (collection_uri,)
        ).fetchone()
        return cast(int, count)

    def _count_above(self, collection_uri: str, position: int) -> int:
        (count,) = self.connection.execute(
            "SELECT COUNT(*) FROM collection_items WHERE collection = ? and position > ?",
            (collection_uri, position),
        ).fetchone()
        return cast(int, count)

    def _next_position(self, collection_uri: str) -> int:
        (position,) = self.connection.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM collection_items WHERE collection = ?",
            (collection_uri,),
        ).fetchone()
        return cast(int, position)

    def _holds(self, collection_uri: str, item_uri: str) -> bool:
        found = self.connection.execute(
            "SELECT 1 FROM collection_items WHERE collection = ? and uri = ? LIMIT 1",
            (collection_uri, item_uri),
        ).fetchone()
        return found is not None

    async def _migrated(self, document: JSONObject) -> JSONObject:
        """Import a legacy item array into the table, then drop the key."""
        key = next((name for name in ITEMS_KEYS if name in document), None)
        if key is None:
            return document
        collection_uri = str(document["id"])
        items = [str(item) for item in cast(list, document.get(key) or [])]
        if items and self._item_count(collection_uri) == 0:
            with self.connection:
                # A stored document holds its items newest first; positions run the other
                # way, so the array's first item takes the highest position.
                self.connection.executemany(
                    "INSERT INTO collection_items (collection, position, uri) VALUES (?,?,?)",
                    [
                        (collection_uri, len(items) - index, item)
                        for index, item in enumerate(items)
                    ],
                )
        migrated = {name: value for name, value in document.items() if name != key}
        migrated["totalItems"] = self._item_count(collection_uri)
        await self.put(migrated)
        return migrated

    async def _collection(self, collection_uri: str) -> JSONObject | None:
        document = await self.get(collection_uri)
        return await self._migrated(document) if document else None

    async def _sync_total(self, collection_uri: str, document: JSONObject) -> int:
        count = self._item_count(collection_uri)
        if document.get("totalItems") != count:
            document["totalItems"] = count
            await self.put(document)
        return count

    async def append_item(self, collection_uri: str, item_uri: str) -> int:
        document = await self._collection(collection_uri)
        if document is None:
            raise UnknownCollectionError(f"Unknown collection: {collection_uri}")
        with self.connection:
            self.connection.execute(
                "INSERT INTO collection_items (collection, position, uri) VALUES (?,?,?) "
                "ON CONFLICT DO NOTHING",
                (collection_uri, self._next_position(collection_uri), item_uri),
            )
        return await self._sync_total(collection_uri, document)

    async def remove_item(self, collection_uri: str, item_uri: str) -> int:
        document = await self._collection(collection_uri)
        if document is None:
            raise UnknownCollectionError(f"Unknown collection: {collection_uri}")
        if not self._holds(collection_uri, item_uri):
            return self._item_count(collection_uri)
        with self.connection:
            self.connection.execute(
                "DELETE FROM collection_items WHERE collection = ? and uri = ?",
                (collection_uri, item_uri),
            )
        return await self._sync_total(collection_uri, document)

    async def contains_item(self, collection_uri: str, item_uri: str) -> bool:
        if await self._collection(collection_uri) is None:
            return False
        return self._holds(collection_uri, item_uri)

    async def count_items(self, collection_uri: str) -> int:
        if await self._collection(collection_uri) is None:
            return 0
        return self._item_count(collection_uri)

    async def page_items(
        self,
        collection_uri: str,
        cursor: str | None = None,
        *,
        start: int | None = None,
        limit: int = DEFAULT_COLLECTION_PAGE_SIZE,
    ) -> CollectionPage:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        limit = min(limit, MAX_COLLECTION_PAGE_SIZE)
        if cursor is not None and start is not None:
            raise ValueError("cursor and start are mutually exclusive")
        if await self._collection(collection_uri) is None:
            return CollectionPage()
        total = self._item_count(collection_uri)
        if cursor is not None:
            token_start = decode_start(cursor)
            if token_start is not None:
                start, cursor = token_start, None
        if start is not None:
            if start < 0:
                raise ValueError("start must not be negative")
            # A page number is answered in one read: `start` is how far into the newest
            # first ordering the page begins, so OFFSET is exact here.
            offset = min(start, total)
            rows = self.connection.execute(
                "SELECT uri, position FROM collection_items WHERE collection = ? "
                "ORDER BY position DESC LIMIT ? OFFSET ?",
                (collection_uri, limit + 1, offset),
            ).fetchall()
            start_index = offset
        else:
            below = None
            if cursor is not None:
                try:
                    below = int(cursor)
                except ValueError:
                    raise ValueError(f"Malformed collection cursor: {cursor}") from None
            sql = "SELECT uri, position FROM collection_items WHERE collection = ?"
            params: list[Any] = [collection_uri]
            if below is not None:
                sql += " and position < ?"
                params.append(below)
            sql += " ORDER BY position DESC LIMIT ?"
            params.append(limit + 1)
            rows = self.connection.execute(sql, params).fetchall()
            start_index = self._count_above(collection_uri, rows[0][1]) if rows else total
        page = rows[:limit]
        next_cursor = str(page[-1][1]) if len(rows) > limit else None
        return CollectionPage(
            items=tuple(str(row[0]) for row in page),
            cursor=next_cursor,
            prev_cursor=encode_start(max(start_index - limit, 0)) if start_index > 0 else None,
            total=total,
            start_index=start_index,
        )
