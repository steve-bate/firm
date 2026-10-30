"""The outstanding units of work: one activity owed to one endpoint.

A queue stores and orders; it decides nothing. Every time in a row is computed by
the caller, so the retry policy lives in one place rather than in two backends.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable, Protocol


class DeliveryState(StrEnum):
    PENDING = "pending"
    INFLIGHT = "inflight"
    DELIVERED = "delivered"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class Delivery:
    """One activity owed to one endpoint.

    ``tenant`` is the tenant that published the activity; ``endpoint`` is the inbox
    it is owed to, which may belong to a different tenant on this instance.
    """

    id: str
    tenant: str
    activity: str
    endpoint: str
    state: DeliveryState = DeliveryState.PENDING
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_reason: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None


class DeliveryQueue(Protocol):
    """The outstanding units of work. No policy: every time is decided by the caller."""

    async def enqueue(self, delivery: Delivery) -> None: ...

    async def claim_due(self, *, now: float, limit: int) -> list[Delivery]: ...

    async def reclaim_stale(self, *, now: float, lease: float) -> int: ...

    async def finish(self, delivery_id: str) -> None: ...

    async def reschedule(
        self, delivery_id: str, *, next_attempt_at: float, reason: str
    ) -> None: ...

    async def abandon(self, delivery_id: str, *, reason: str) -> None: ...

    async def get(self, delivery_id: str) -> Delivery | None: ...

    async def list(
        self, *, state: DeliveryState | None = None, limit: int = 100
    ) -> list[Delivery]: ...

    async def counts(self) -> dict[str, int]: ...

    async def prune(self, *, now: float, history_ttl: float) -> int: ...

    async def close(self) -> None: ...


class MemoryDeliveryQueue:
    """The same contract in a dict, for tests and for a memory-backed server."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._rows: dict[str, Delivery] = {}
        self._clock = clock

    async def enqueue(self, delivery: Delivery) -> None:
        self._rows[delivery.id] = delivery

    async def claim_due(self, *, now: float, limit: int) -> list[Delivery]:
        due = sorted(
            (
                row
                for row in self._rows.values()
                if row.state is DeliveryState.PENDING and row.next_attempt_at <= now
            ),
            key=lambda row: (row.next_attempt_at, row.id),
        )[:limit]
        for row in due:
            self._rows[row.id] = replace(row, state=DeliveryState.INFLIGHT, started_at=now)
        return [self._rows[row.id] for row in due]

    async def reclaim_stale(self, *, now: float, lease: float) -> int:
        reclaimed = 0
        for row in list(self._rows.values()):
            if (
                row.state is DeliveryState.INFLIGHT
                and row.started_at is not None
                and row.started_at < now - lease
            ):
                self._rows[row.id] = replace(
                    row, state=DeliveryState.PENDING, started_at=None, next_attempt_at=now
                )
                reclaimed += 1
        return reclaimed

    async def finish(self, delivery_id: str) -> None:
        row = self._rows.get(delivery_id)
        if row is None:
            return
        self._rows[delivery_id] = replace(
            row,
            state=DeliveryState.DELIVERED,
            started_at=None,
            finished_at=self._clock(),
            last_reason=None,
        )

    async def reschedule(self, delivery_id: str, *, next_attempt_at: float, reason: str) -> None:
        row = self._rows.get(delivery_id)
        if row is None:
            return
        self._rows[delivery_id] = replace(
            row,
            state=DeliveryState.PENDING,
            attempts=row.attempts + 1,
            next_attempt_at=next_attempt_at,
            last_reason=reason,
            started_at=None,
        )

    async def abandon(self, delivery_id: str, *, reason: str) -> None:
        row = self._rows.get(delivery_id)
        if row is None:
            return
        self._rows[delivery_id] = replace(
            row,
            state=DeliveryState.ABANDONED,
            last_reason=reason,
            started_at=None,
            finished_at=self._clock(),
        )

    async def get(self, delivery_id: str) -> Delivery | None:
        return self._rows.get(delivery_id)

    async def list(self, *, state: DeliveryState | None = None, limit: int = 100) -> list[Delivery]:
        rows = [row for row in self._rows.values() if state is None or row.state is state]
        rows.sort(key=lambda row: (row.created_at, row.id))
        return rows[:limit]

    async def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {state.value: 0 for state in DeliveryState}
        for row in self._rows.values():
            counts[row.state.value] += 1
        return counts

    async def prune(self, *, now: float, history_ttl: float) -> int:
        stale = [
            row.id
            for row in self._rows.values()
            if row.state is DeliveryState.DELIVERED
            and row.finished_at is not None
            and row.finished_at < now - history_ttl
        ]
        for delivery_id in stale:
            del self._rows[delivery_id]
        return len(stale)

    async def close(self) -> None:
        return None
