import hashlib
import json
import sqlite3
from contextlib import closing
from functools import lru_cache
from typing import Any, cast

from firm.interfaces import JSONObject, QueryCriteria, ResourceStore


class SqliteResourceStore(ResourceStore):
    def __init__(self, name: str, db: str):
        self.name = name
        self.connection = sqlite3.connect(db)
        self._initialize_table()

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _initialize_table(self) -> None:
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS objects (
                partition TEXT NOT NULL,
                uri TEXT NOT NULL,
                object JSON NOT NULL,
                PRIMARY KEY (partition, uri)
            )
            """
            )

    @lru_cache
    def _hash(self, uri: str) -> str:
        md5 = hashlib.md5()
        md5.update(uri.encode())
        return md5.hexdigest()

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
        await self.remove(uri)
        with closing(self.connection.cursor()) as cursor:
            # Remove existing object
            data = json.dumps(resource)
            cursor.execute(
                "INSERT INTO objects (partition, uri, object) VALUES (?,?,?)",
                (self.name, uri, data),
            )
            self.connection.commit()

    async def remove(self, uri: str) -> None:
        """Remove an object from the store"""
        with closing(self.connection.cursor()) as cursor:
            # Remove existing object
            cursor.execute(
                "DELETE FROM objects WHERE partition = ? and uri = ?",
                (self.name, uri),
            )
            self.connection.commit()

    # NOTE This API will change
    async def query(self, criteria: QueryCriteria) -> list[JSONObject]:
        field_criteria = " and ".join(
            (
                f"(json_extract(object, '$.{key}') = '{value}' "
                f"or (json_type(object, '$.{key}') = 'array' and "
                f"'{value}' in (select value from json_each(object, '$.{key}')))"
                ")"
            )
            for key, value in criteria.items()
        )
        sql = f"SELECT object FROM objects WHERE partition = ? and {field_criteria}"
        with closing(self.connection.cursor()) as cursor:
            rows = cursor.execute(sql, (self.name,)).fetchmany(100)
            return [json.loads(row[0]) for row in rows] if rows else []
