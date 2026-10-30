"""The delivery queue contract: one activity owed to one endpoint."""

from dataclasses import replace
from typing import Any

import pytest

from firm.core.delivery.queue import Delivery, DeliveryState, MemoryDeliveryQueue
from firm.core.delivery.sqlite import SqliteDeliveryQueue

TENANT = "https://local.test"
ACTIVITY = f"{TENANT}/activities/1"
ENDPOINT = "https://remote.test/users/bob/inbox"


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=["memory", "sqlite"])
async def queue(request, clock, tmp_path):
    """Both backends answer the same contract, unmodified."""
    if request.param == "memory":
        yield MemoryDeliveryQueue(clock=clock)
        return
    sqlite = SqliteDeliveryQueue(tmp_path / "delivery.sqlite", clock=clock)
    yield sqlite
    await sqlite.close()


def a_delivery(**overrides: Any) -> Delivery:
    return replace(
        Delivery(
            id="d1",
            tenant=TENANT,
            activity=ACTIVITY,
            endpoint=ENDPOINT,
            created_at=1_000.0,
            next_attempt_at=1_000.0,
        ),
        **overrides,
    )


async def test_enqueue_then_claim_due_returns_the_unit(queue):
    await queue.enqueue(a_delivery())

    claimed = await queue.claim_due(now=1_000.0, limit=10)

    assert [row.id for row in claimed] == ["d1"]
    assert claimed[0].state is DeliveryState.INFLIGHT
    assert claimed[0].started_at == 1_000.0


async def test_a_unit_is_not_due_before_its_next_attempt(queue):
    await queue.enqueue(a_delivery(next_attempt_at=2_000.0))

    assert await queue.claim_due(now=1_000.0, limit=10) == []


async def test_finish_marks_it_delivered_and_never_due_again(queue, clock):
    await queue.enqueue(a_delivery())
    await queue.claim_due(now=1_000.0, limit=10)

    await queue.finish("d1")

    row = await queue.get("d1")
    assert row.state is DeliveryState.DELIVERED
    assert row.finished_at == clock.now
    assert await queue.claim_due(now=9_999.0, limit=10) == []


async def test_reschedule_counts_the_attempt_and_defers_it(queue):
    await queue.enqueue(a_delivery())
    await queue.claim_due(now=1_000.0, limit=10)

    await queue.reschedule("d1", next_attempt_at=1_030.0, reason="HTTP 503")

    row = await queue.get("d1")
    assert row.state is DeliveryState.PENDING
    assert row.attempts == 1
    assert row.next_attempt_at == 1_030.0
    assert row.last_reason == "HTTP 503"
    assert await queue.claim_due(now=1_029.0, limit=10) == []


async def test_abandon_is_terminal(queue, clock):
    await queue.enqueue(a_delivery())

    await queue.abandon("d1", reason="HTTP 403")

    row = await queue.get("d1")
    assert row.state is DeliveryState.ABANDONED
    assert row.last_reason == "HTTP 403"
    assert row.finished_at == clock.now
    assert await queue.claim_due(now=9_999.0, limit=10) == []


async def test_reclaim_stale_returns_a_dead_workers_unit_to_pending(queue):
    await queue.enqueue(a_delivery())
    await queue.claim_due(now=1_000.0, limit=10)

    reclaimed = await queue.reclaim_stale(now=1_400.0, lease=300.0)

    row = await queue.get("d1")
    assert reclaimed == 1
    assert row.state is DeliveryState.PENDING
    assert row.started_at is None
    assert row.next_attempt_at == 1_400.0


async def test_list_filters_by_state(queue):
    await queue.enqueue(a_delivery(id="a"))
    await queue.enqueue(a_delivery(id="b"))
    await queue.claim_due(now=1_000.0, limit=10)
    await queue.finish("a")

    assert [row.id for row in await queue.list(state=DeliveryState.DELIVERED)] == ["a"]
    assert [row.id for row in await queue.list(state=DeliveryState.INFLIGHT)] == ["b"]


async def test_counts_by_state(queue):
    await queue.enqueue(a_delivery(id="a"))
    await queue.enqueue(a_delivery(id="b"))
    await queue.claim_due(now=1_000.0, limit=10)
    await queue.finish("a")
    await queue.abandon("b", reason="HTTP 404")

    assert await queue.counts() == {
        "pending": 0,
        "inflight": 0,
        "delivered": 1,
        "abandoned": 1,
    }


async def test_prune_drops_delivered_history_past_the_ttl(queue):
    await queue.enqueue(a_delivery())
    await queue.claim_due(now=1_000.0, limit=10)
    await queue.finish("d1")

    assert await queue.prune(now=1_100.0, history_ttl=3_600.0) == 0
    assert await queue.prune(now=6_000.0, history_ttl=3_600.0) == 1
    assert await queue.get("d1") is None
