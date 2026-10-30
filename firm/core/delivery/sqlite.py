"""The delivery queue in SQLite, so outstanding work survives a restart."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import aiosqlite

from firm.core.delivery.queue import Delivery, DeliveryState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    id              TEXT PRIMARY KEY,
    tenant          TEXT NOT NULL,
    activity        TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    state           TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_reason     TEXT,
    created_at      REAL NOT NULL DEFAULT 0,
    started_at      REAL,
    finished_at     REAL
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries (state, next_attempt_at);
"""

_COLUMNS = (
    "id, tenant, activity, endpoint, state, attempts, next_attempt_at, "
    "last_reason, created_at, started_at, finished_at"
)


class SqliteDeliveryQueue:
    """The delivery queue in one file, claimed under ``BEGIN IMMEDIATE``."""

    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._db: aiosqlite.Connection | None = None
        self._opening = asyncio.Lock()

    async def _connection(self) -> aiosqlite.Connection:
        async with self._opening:
            if self._db is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                db = await aiosqlite.connect(self._path, isolation_level=None)
                db.row_factory = aiosqlite.Row
                await db.executescript(_SCHEMA)
                self._db = db
        return self._db

    async def enqueue(self, delivery: Delivery) -> None:
        """Insert the unit once; a second attempt at the same work changes nothing."""
        db = await self._connection()
        await db.execute(
            f"INSERT OR IGNORE INTO deliveries ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                delivery.id,
                delivery.tenant,
                delivery.activity,
                delivery.endpoint,
                delivery.state.value,
                delivery.attempts,
                delivery.next_attempt_at,
                delivery.last_reason,
                delivery.created_at,
                delivery.started_at,
                delivery.finished_at,
            ),
        )
        await db.commit()

    async def claim_due(self, *, now: float, limit: int) -> list[Delivery]:
        db = await self._connection()
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT id FROM deliveries WHERE state = ? AND next_attempt_at <= ?"
                " ORDER BY next_attempt_at, id LIMIT ?",
                (DeliveryState.PENDING.value, now, limit),
            )
            ids = [row["id"] for row in await cursor.fetchall()]
            if ids:
                marks = ",".join("?" for _ in ids)
                await db.execute(
                    f"UPDATE deliveries SET state = ?, started_at = ?"
                    f" WHERE id IN ({marks}) AND state = ?",
                    (DeliveryState.INFLIGHT.value, now, *ids, DeliveryState.PENDING.value),
                )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        cursor = await db.execute(
            f"SELECT {_COLUMNS} FROM deliveries WHERE id IN ({marks})"
            " ORDER BY next_attempt_at, id",
            ids,
        )
        return [_to_delivery(row) for row in await cursor.fetchall()]

    async def reclaim_stale(self, *, now: float, lease: float) -> int:
        db = await self._connection()
        cursor = await db.execute(
            "UPDATE deliveries SET state = ?, started_at = NULL, next_attempt_at = ?"
            " WHERE state = ? AND started_at IS NOT NULL AND started_at < ?",
            (
                DeliveryState.PENDING.value,
                now,
                DeliveryState.INFLIGHT.value,
                now - lease,
            ),
        )
        await db.commit()
        return cursor.rowcount

    async def finish(self, delivery_id: str) -> None:
        db = await self._connection()
        await db.execute(
            "UPDATE deliveries SET state = ?, started_at = NULL, finished_at = ?,"
            " last_reason = NULL WHERE id = ?",
            (DeliveryState.DELIVERED.value, self._clock(), delivery_id),
        )
        await db.commit()

    async def reschedule(self, delivery_id: str, *, next_attempt_at: float, reason: str) -> None:
        db = await self._connection()
        await db.execute(
            "UPDATE deliveries SET state = ?, attempts = attempts + 1,"
            " next_attempt_at = ?, last_reason = ?, started_at = NULL WHERE id = ?",
            (DeliveryState.PENDING.value, next_attempt_at, reason, delivery_id),
        )
        await db.commit()

    async def abandon(self, delivery_id: str, *, reason: str, attempts: int | None = None) -> None:
        db = await self._connection()
        await db.execute(
            "UPDATE deliveries SET state = ?, attempts = COALESCE(?, attempts),"
            " last_reason = ?, started_at = NULL, finished_at = ? WHERE id = ?",
            (
                DeliveryState.ABANDONED.value,
                attempts,
                reason,
                self._clock(),
                delivery_id,
            ),
        )
        await db.commit()

    async def get(self, delivery_id: str) -> Delivery | None:
        db = await self._connection()
        cursor = await db.execute(f"SELECT {_COLUMNS} FROM deliveries WHERE id = ?", (delivery_id,))
        row = await cursor.fetchone()
        return _to_delivery(row) if row is not None else None

    async def list(self, *, state: DeliveryState | None = None, limit: int = 100) -> list[Delivery]:
        db = await self._connection()
        if state is None:
            cursor = await db.execute(
                f"SELECT {_COLUMNS} FROM deliveries ORDER BY created_at, id LIMIT ?", (limit,)
            )
        else:
            cursor = await db.execute(
                f"SELECT {_COLUMNS} FROM deliveries WHERE state = ?"
                " ORDER BY created_at, id LIMIT ?",
                (state.value, limit),
            )
        return [_to_delivery(row) for row in await cursor.fetchall()]

    async def counts(self) -> dict[str, int]:
        db = await self._connection()
        cursor = await db.execute("SELECT state, COUNT(*) AS n FROM deliveries GROUP BY state")
        counts = {state.value: 0 for state in DeliveryState}
        for row in await cursor.fetchall():
            counts[row["state"]] = row["n"]
        return counts

    async def prune(self, *, now: float, history_ttl: float, abandoned: bool = False) -> int:
        db = await self._connection()
        states = [DeliveryState.DELIVERED.value]
        if abandoned:
            states.append(DeliveryState.ABANDONED.value)
        marks = ",".join("?" for _ in states)
        cursor = await db.execute(
            f"DELETE FROM deliveries WHERE state IN ({marks}) AND finished_at IS NOT NULL"
            " AND finished_at < ?",
            (*states, now - history_ttl),
        )
        await db.commit()
        return cursor.rowcount

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None


def _to_delivery(row: aiosqlite.Row) -> Delivery:
    return Delivery(
        id=row["id"],
        tenant=row["tenant"],
        activity=row["activity"],
        endpoint=row["endpoint"],
        state=DeliveryState(row["state"]),
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        last_reason=row["last_reason"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
