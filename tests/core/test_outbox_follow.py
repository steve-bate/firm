"""An outbound `Follow` enters `following` at once, as Mastodon does.

The relationship has to be visible before the remote server answers: until the
`Accept` arrives there is no other document that says the actor asked to follow
anyone, and a client that reloads its following list in that window shows nothing.
`Reject` and `Undo` remove the actor again, so the collection is only optimistic,
never wrong for long.
"""

from typing import cast

import pytest

from firm.core.interfaces import APActor, JSONObject, Tenant
from firm.core.services.activitypub.exceptions import InvalidRequestException
from firm.core.services.activitypub.inbox import _process_inbox_reject
from firm.core.services.activitypub.outbox import (
    _process_outbox_follow,
    _process_outbox_undo_follow,
    process_outbox,
)
from firm.core.services.activitypub.support import FirmBoxContext
from firm.core.store.memory import MemoryResourceStore
from tests.support import StubDeliveryService, make_context

PREFIX = "http://tenant1.test"
ACTOR = f"{PREFIX}/user1"
FOLLOWING = f"{ACTOR}/following"
FOLLOWER = "http://remote.test/users/alice"
FOLLOW = f"{ACTOR}/follow/1"


@pytest.fixture
def tenant(tmp_path) -> Tenant:
    return Tenant(PREFIX, MemoryResourceStore(), MemoryResourceStore(), tmp_path)


async def _seed(tenant: Tenant) -> None:
    await tenant.public_store.put({"id": FOLLOWING, "type": "Collection", "totalItems": 0})
    await tenant.public_store.put(
        {"id": f"{ACTOR}/outbox", "type": "OrderedCollection", "totalItems": 0}
    )


def _actor() -> JSONObject:
    return {
        "id": ACTOR,
        "type": "Person",
        "inbox": f"{ACTOR}/inbox",
        "outbox": f"{ACTOR}/outbox",
        "followers": f"{ACTOR}/followers",
        "following": FOLLOWING,
    }


def _context(tenant: Tenant) -> FirmBoxContext:
    base = make_context(tenant)
    return FirmBoxContext(
        base.resolver,
        tenant,
        base.principal,
        base.authorizer,
        box_owner=cast(APActor, _actor()),
        box_uri=f"{ACTOR}/outbox",
        delivery_service=StubDeliveryService(),
    )


def _follow() -> JSONObject:
    return {"id": FOLLOW, "type": "Follow", "actor": ACTOR, "object": FOLLOWER}


async def _following(tenant: Tenant) -> list[str]:
    page = await tenant.public_store.page_items(FOLLOWING, None, limit=100)
    return list(page.items)


async def test_outbox_dispatch_routes_a_follow_to_the_handler(tenant: Tenant):
    """The dispatch in `process_outbox` is what a client actually hits: follow, no Accept."""
    await _seed(tenant)

    await process_outbox(_context(tenant), _follow())

    assert FOLLOWER in await _following(tenant)


async def test_follow_adds_the_actor_before_any_accept(tenant: Tenant):
    await _seed(tenant)

    await _process_outbox_follow(_context(tenant), _follow())

    assert FOLLOWER in await _following(tenant)


async def test_second_follow_of_the_same_actor_is_not_duplicated(tenant: Tenant):
    """The collection is a set: a repeat Follow must not add a second copy."""
    await _seed(tenant)

    await _process_outbox_follow(_context(tenant), _follow())
    await _process_outbox_follow(_context(tenant), _follow())

    assert await _following(tenant) == [FOLLOWER]


async def test_follow_without_an_object_is_rejected(tenant: Tenant):
    await _seed(tenant)

    with pytest.raises(InvalidRequestException):
        await _process_outbox_follow(_context(tenant), {"id": FOLLOW, "type": "Follow"})


async def test_undo_removes_the_optimistic_entry(tenant: Tenant):
    await _seed(tenant)

    await _process_outbox_follow(_context(tenant), _follow())
    follow = _follow()
    await tenant.public_store.put(follow)

    await _process_outbox_undo_follow(_context(tenant), follow)

    assert await _following(tenant) == []


async def test_reject_removes_the_optimistic_entry(tenant: Tenant):
    """A refused follow must not leave the actor in `following`."""
    await _seed(tenant)
    await _process_outbox_follow(_context(tenant), _follow())
    await tenant.public_store.put(_follow())

    reject: JSONObject = {
        "id": f"{FOLLOWER}/reject/1",
        "type": "Reject",
        "actor": FOLLOWER,
        "object": FOLLOW,
    }
    await _process_inbox_reject(_context(tenant), reject)

    assert await _following(tenant) == []
