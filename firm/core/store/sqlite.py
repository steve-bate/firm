import json
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from typing import Any, cast

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    DEFAULT_SEARCH_PAGE_SIZE,
    DEFAULT_TEXT_FIELDS,
    MAX_COLLECTION_PAGE_SIZE,
    CollectionPage,
    JSONObject,
    QueryCriteria,
    SearchPage,
    UnknownCollectionError,
    decode_start,
    encode_start,
)
from firm.core.store.base import ResourceStoreBase
from firm.core.store.collection_items import insertion_index
from firm.core.util import is_ordered_collection, normalize_collection_items
from firm.search.parser import parse_query
from firm.search.transform.sql import ast_to_sql

TYPE_COLUMN = """COALESCE(
                (SELECT value FROM json_each(object, '$.type')
                 WHERE json_each.type = 'text' ORDER BY json_each.key LIMIT 1),
                json_extract(object, '$.type')
            ) AS type"""


def _regexp(pattern: str, value: Any) -> int:
    """SQLite has no REGEXP operator; the search SQL calls this one."""
    if value is None:
        return 0
    return 1 if re.search(pattern, str(value)) else 0


class SqliteResourceStore(ResourceStoreBase):
    def __init__(self, name: str, db: str):
        self.name = name
        self.connection = sqlite3.connect(db)
        self.connection.create_function("regexp", 2, _regexp)
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
        normalize_collection_items(resource)
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
        """Import a legacy item array into the table, then drop the key.

        The array may sit under "items" (written after normalisation) or under
        "orderedItems" (written by an older build). Both are read here because this is
        an ingest point; the document leaves without either key.
        """
        legacy = document.get("items")
        if legacy is None:
            legacy = document.get("orderedItems")
        if legacy is None:
            return document
        collection_uri = str(document["id"])
        items = [str(item) for item in cast(list, legacy)]
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
        migrated = {
            name: value for name, value in document.items() if name not in ("items", "orderedItems")
        }
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

    async def insert_item(self, collection_uri: str, item_uri: str, *, offset: int = 0) -> int:
        """Insert an item at ``offset``; positions ascend towards newer items.

        Offset 0 takes a position above every other (one INSERT), offset -1 one below every
        other (one INSERT), and any other offset renumbers the collection. A Collection has
        no order, so its offset is ignored and the cheap INSERT is used.
        """
        document = await self._collection(collection_uri)
        if document is None:
            raise UnknownCollectionError(f"Unknown collection: {collection_uri}")
        if not is_ordered_collection(document) or offset == 0:
            self._insert_at(collection_uri, self._next_position(collection_uri), item_uri)
        elif offset == -1:
            self._insert_at(collection_uri, self._lowest_position(collection_uri) - 1, item_uri)
        else:
            self._insert_at_index(collection_uri, item_uri, offset)
        return await self._sync_total(collection_uri, document)

    def _lowest_position(self, collection_uri: str) -> int:
        (low,) = self.connection.execute(
            "SELECT COALESCE(MIN(position), 1) FROM collection_items WHERE collection = ?",
            (collection_uri,),
        ).fetchone()
        return cast(int, low)

    def _insert_at(self, collection_uri: str, position: int, item_uri: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO collection_items (collection, position, uri) VALUES (?,?,?) "
                "ON CONFLICT DO NOTHING",
                (collection_uri, position, item_uri),
            )

    def _insert_at_index(self, collection_uri: str, item_uri: str, offset: int) -> None:
        """Renumber the collection so the item lands before served index ``offset``."""
        rows = self.connection.execute(
            "SELECT uri, position FROM collection_items WHERE collection = ? "
            "ORDER BY position DESC",
            (collection_uri,),
        ).fetchall()
        uris = [str(row[0]) for row in rows]
        index = insertion_index(len(uris), offset)
        uris.insert(index, item_uri)
        top = max((int(row[1]) for row in rows), default=0)
        with self.connection:
            self.connection.execute(
                "DELETE FROM collection_items WHERE collection = ?", (collection_uri,)
            )
            self.connection.executemany(
                "INSERT INTO collection_items (collection, position, uri) VALUES (?,?,?)",
                [(collection_uri, top - i, uri) for i, uri in enumerate(uris)],
            )

    async def search(
        self,
        query: str,
        *,
        text_fields: Sequence[str] = DEFAULT_TEXT_FIELDS,
        start: int = 0,
        limit: int = DEFAULT_SEARCH_PAGE_SIZE,
    ) -> SearchPage:
        """Compile the query to SQL over the partition's documents.

        ``ast_to_sql`` writes clauses against two columns — ``document``, the stored JSON,
        and ``type``, its first textual type — so each query runs over a CTE that presents
        the partition in that shape. Ordering is by uri, which makes a window stable.
        """
        where, params = ast_to_sql(parse_query(query), text_facet_names=list(text_fields))
        documents_cte = (
            "WITH docs AS ("
            f"SELECT object AS document, {TYPE_COLUMN}, uri AS uri "
            "FROM objects WHERE partition = ?"
            ")"
        )
        with closing(self.connection.cursor()) as cursor:
            (total,) = cursor.execute(
                f"{documents_cte} SELECT count(*) FROM docs WHERE {where}",
                [self.name, *params],
            ).fetchone()
            rows = cursor.execute(
                f"{documents_cte} SELECT document FROM docs WHERE {where} ORDER BY uri LIMIT ? OFFSET ?",
                [self.name, *params, limit, start],
            ).fetchall()
        return SearchPage(
            documents=tuple(json.loads(row[0]) for row in rows),
            total=cast(int, total),
            start_index=start,
        )

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
