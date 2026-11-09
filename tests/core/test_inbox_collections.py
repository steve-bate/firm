"""A Like of one of this server's objects never depends on a collection that object has.

A note created through the outbox is stored as the client sent it, so it names no ``likes``
collection; snac2 liked such a note and firm answered 500 with ``KeyError: 'likes'``, then
kept answering 500 on every retry. The collection is now minted by the first activity that
needs it, and an object owned elsewhere still keeps its origin's.
"""

from typing import cast

import pytest

from firm.core.interfaces import APActor, JSONObject, Tenant
from firm.core.remote.cache import MemoryRemoteCache
from firm.core.remote.provider import FetchPolicy, RemoteResourceProvider
from firm.core.services.activitypub.inbox import (
    _process_inbox_announce,
    _process_inbox_like,
    _process_inbox_undo_like,
)
from firm.core.services.activitypub.support import FirmBoxContext
from firm.core.store.memory import MemoryResourceStore
from tests.support import (
    StubDeliveryService,
    StubTransport,
    json_response,
    make_context,
)

PREFIX = "http://tenant1.test"
ACTOR = f"{PREFIX}/user1"
NOTE = f"{PREFIX}/note/1"
REMOTE_NOTE = "http://remote.test/users/bob/p/1"
REMOTE_LIKES = "http://remote.test/users/bob/p/1/likes"
SHARES = f"{NOTE}/shares"
LIKES = f"{NOTE}/likes"


@pytest.fixture
def tenant(tmp_path) -> Tenant:
    return Tenant(PREFIX, MemoryResourceStore(), MemoryResourceStore(), tmp_path)


def _actor() -> JSONObject:
    return {
        "id": ACTOR,
        "type": "Person",
        "inbox": f"{ACTOR}/inbox",
        "outbox": f"{ACTOR}/outbox",
        "followers": f"{ACTOR}/followers",
        "following": f"{ACTOR}/following",
        "liked": f"{ACTOR}/liked",
    }


def _context(tenant: Tenant, *, remote: RemoteResourceProvider | None = None) -> FirmBoxContext:
    base = make_context(tenant, remote=remote)
    return FirmBoxContext(
        base.resolver,
        tenant,
        base.principal,
        base.authorizer,
        box_owner=cast(APActor, _actor()),
        box_uri=f"{ACTOR}/inbox",
        delivery_service=StubDeliveryService(),
    )


def _provider(transport: StubTransport) -> RemoteResourceProvider:
    return RemoteResourceProvider(
        MemoryRemoteCache(), transport, policy=FetchPolicy(allow_private_addresses=True)
    )


def _note() -> JSONObject:
    """A note as `_store_create` leaves it: no likes, no shares."""
    return {
        "id": NOTE,
        "type": "Note",
        "attributedTo": ACTOR,
        "content": "What's up, Bob?",
        "to": ["http://remote.test/users/bob"],
        "cc": [],
    }


def _like(object_: str = NOTE) -> JSONObject:
    return {
        "id": f"{PREFIX}/like/1",
        "type": "Like",
        "actor": "http://remote.test/users/bob",
        "object": object_,
    }


def _undo() -> JSONObject:
    return {
        "id": f"{PREFIX}/undo/1",
        "type": "Undo",
        "actor": "http://remote.test/users/bob",
        "object": _like(),
    }


async def _items(tenant: Tenant, uri: str) -> list[str]:
    page = await tenant.public_store.page_items(uri, None, limit=100)
    return list(page.items)


async def test_like_creates_the_likes_collection_a_note_was_stored_without(tenant: Tenant):
    await tenant.public_store.put(_note())

    await _process_inbox_like(_context(tenant), _like())

    collection = await tenant.public_store.get(LIKES)
    assert collection is not None, "the like minted the collection rather than raising"
    assert collection["type"] == "OrderedCollection"
    assert await _items(tenant, LIKES) == ["http://remote.test/users/bob"]

    note = await tenant.public_store.get(NOTE)
    assert note is not None
    assert note["likes"] == LIKES, "the object names the collection it now has"


async def test_a_second_like_by_the_same_actor_does_not_count_twice(tenant: Tenant):
    await tenant.public_store.put(_note())

    await _process_inbox_like(_context(tenant), _like())
    await _process_inbox_like(_context(tenant), _like())

    assert await _items(tenant, LIKES) == ["http://remote.test/users/bob"]


async def test_like_of_a_foreign_object_writes_nothing_here(tenant: Tenant):
    transport = StubTransport(
        {
            REMOTE_NOTE: json_response(
                {"id": REMOTE_NOTE, "type": "Note", "attributedTo": "http://remote.test/users/bob"}
            )
        }
    )

    await _process_inbox_like(_context(tenant, remote=_provider(transport)), _like(REMOTE_NOTE))

    assert not await tenant.public_store.is_stored(f"{REMOTE_NOTE}/likes")


async def test_undo_of_a_like_no_collection_records_is_a_noop(tenant: Tenant):
    """The sender must not be told to retry an Undo for a like this server never kept."""
    await tenant.public_store.put(_note())

    await _process_inbox_undo_like(_context(tenant), _undo())

    assert not await tenant.public_store.is_stored(LIKES), "nothing was created to remove from"


async def test_undo_removes_the_actor_from_an_existing_collection(tenant: Tenant):
    await tenant.public_store.put(_note())
    await _process_inbox_like(_context(tenant), _like())

    await _process_inbox_undo_like(_context(tenant), _undo())

    assert await _items(tenant, LIKES) == []


async def test_announce_still_mints_a_missing_shares_collection(tenant: Tenant):
    """The like path was taught what the announce path already did; announce keeps working."""
    await tenant.public_store.put(_note())
    announce: JSONObject = {
        "id": f"{PREFIX}/announce/1",
        "type": "Announce",
        "actor": "http://remote.test/users/bob",
        "object": NOTE,
    }

    await _process_inbox_announce(_context(tenant), announce)

    collection = await tenant.public_store.get(SHARES)
    assert collection is not None
    assert await _items(tenant, SHARES) == [f"{PREFIX}/announce/1"], "shares holds the activity"

    note = await tenant.public_store.get(NOTE)
    assert note is not None
    assert note["shares"] == SHARES
