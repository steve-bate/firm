"""An outbound `Like` resolves the liked object the way every other read resolves it.

A post the actor met in the federated timeline lives in this server's remote cache, not in
the tenant's store. Looking the Like's object up in the store alone rejected every such Like
with `Unknown liked object` before anything was published, so the client saw a 400 and the
peer saw nothing. The resolver is the reader that knows about both places.
"""

from typing import cast

import pytest

from firm.core.interfaces import APActor, JSONObject, Tenant
from firm.core.remote.cache import MemoryRemoteCache
from firm.core.remote.provider import FetchPolicy, RemoteResourceProvider
from firm.core.services.activitypub.exceptions import InvalidRequestException
from firm.core.services.activitypub.outbox import _process_outbox_like, process_outbox
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
LIKED = f"{ACTOR}/liked"
OUTBOX = f"{ACTOR}/outbox"
NOTE = f"{PREFIX}/note/1"
REMOTE_NOTE = "http://remote.test/p/alice/1"
REMOTE_LIKES = "http://remote.test/p/alice/1/likes"


@pytest.fixture
def tenant(tmp_path) -> Tenant:
    return Tenant(PREFIX, MemoryResourceStore(), MemoryResourceStore(), tmp_path)


def _actor() -> JSONObject:
    return {
        "id": ACTOR,
        "type": "Person",
        "inbox": f"{ACTOR}/inbox",
        "outbox": OUTBOX,
        "followers": f"{ACTOR}/followers",
        "following": f"{ACTOR}/following",
        "liked": LIKED,
    }


def _like(object_: str = REMOTE_NOTE) -> JSONObject:
    return {
        "id": f"{ACTOR}/like/1",
        "type": "Like",
        "actor": ACTOR,
        "object": object_,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
    }


def _context(tenant: Tenant, *, remote: RemoteResourceProvider | None = None) -> FirmBoxContext:
    base = make_context(tenant, remote=remote)
    return FirmBoxContext(
        base.resolver,
        tenant,
        base.principal,
        base.authorizer,
        box_owner=cast(APActor, _actor()),
        box_uri=OUTBOX,
        delivery_service=StubDeliveryService(),
    )


def _provider(transport: StubTransport) -> RemoteResourceProvider:
    return RemoteResourceProvider(
        MemoryRemoteCache(), transport, policy=FetchPolicy(allow_private_addresses=True)
    )


async def _seed(tenant: Tenant) -> None:
    await tenant.public_store.put(_actor())
    await tenant.public_store.put({"id": LIKED, "type": "Collection", "totalItems": 0})
    await tenant.public_store.put({"id": OUTBOX, "type": "OrderedCollection", "totalItems": 0})


async def _items(tenant: Tenant, uri: str) -> list[str]:
    page = await tenant.public_store.page_items(uri, None, limit=100)
    return list(page.items)


async def test_like_of_a_remote_post_is_recorded_on_the_actors_liked_collection(tenant: Tenant):
    await _seed(tenant)
    transport = StubTransport({REMOTE_NOTE: json_response({"id": REMOTE_NOTE, "type": "Note"})})

    await _process_outbox_like(_context(tenant, remote=_provider(transport)), _like())

    assert await _items(tenant, LIKED) == [REMOTE_NOTE]
    assert not await tenant.public_store.is_stored(REMOTE_NOTE), "a foreign URI stays foreign"


async def test_a_resolved_like_is_published_rather_than_rejected(tenant: Tenant):
    """What the client was missing: the activity leaves, instead of a 400 before delivery."""
    await _seed(tenant)
    transport = StubTransport({REMOTE_NOTE: json_response({"id": REMOTE_NOTE, "type": "Note"})})
    context = _context(tenant, remote=_provider(transport))

    await process_outbox(context, _like())

    delivery = cast(StubDeliveryService, context.delivery_service)
    assert delivery.delivered, "the Like is handed to the delivery service"
    assert delivery.activities[0]["type"] == "Like"
    assert delivery.activities[0]["object"] == REMOTE_NOTE
    assert REMOTE_NOTE in await _items(tenant, LIKED)
    assert delivery.delivered[0] in await _items(tenant, OUTBOX)


async def test_like_of_an_unresolvable_object_is_rejected(tenant: Tenant):
    await _seed(tenant)

    with pytest.raises(InvalidRequestException):
        await _process_outbox_like(_context(tenant, remote=None), _like())


async def test_like_of_a_local_post_updates_its_likes_collection(tenant: Tenant):
    await _seed(tenant)
    await tenant.public_store.put(
        {"id": NOTE, "type": "Note", "likes": f"{NOTE}/likes", "attributedTo": ACTOR}
    )
    await tenant.public_store.put({"id": f"{NOTE}/likes", "type": "Collection", "totalItems": 0})

    await _process_outbox_like(_context(tenant), _like(NOTE))

    assert await _items(tenant, f"{NOTE}/likes") == [ACTOR]
    assert await _items(tenant, LIKED) == [NOTE]


async def test_like_of_a_local_post_that_names_no_collection_mints_one(tenant: Tenant):
    """A note stored before it had a collection is repaired by the like that needs it."""
    await _seed(tenant)
    await tenant.public_store.put({"id": NOTE, "type": "Note", "attributedTo": ACTOR})

    await _process_outbox_like(_context(tenant), _like(NOTE))

    collection = await tenant.public_store.get(f"{NOTE}/likes")
    assert collection is not None
    assert await _items(tenant, f"{NOTE}/likes") == [ACTOR]
    note = await tenant.public_store.get(NOTE)
    assert note is not None and note["likes"] == f"{NOTE}/likes"


async def test_a_remote_objects_likes_collection_is_not_written_here(tenant: Tenant):
    """A federated `likes` IRI names the origin's collection; it is never ours to keep."""
    await _seed(tenant)
    transport = StubTransport(
        {REMOTE_NOTE: json_response({"id": REMOTE_NOTE, "type": "Note", "likes": REMOTE_LIKES})}
    )

    await _process_outbox_like(_context(tenant, remote=_provider(transport)), _like())

    assert not await tenant.public_store.is_stored(REMOTE_LIKES)
    assert await _items(tenant, LIKED) == [REMOTE_NOTE]
