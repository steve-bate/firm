"""`Add` and `Remove` on an outbox write the target collection through the store.

The activity's `target` is an IRI, as a client sends it, so the handler resolves the
collection from the store before touching it.
"""

from typing import cast

import pytest

from firm.core.interfaces import APActor, JSONObject, Tenant
from firm.core.services.activitypub.exceptions import InvalidResourceException
from firm.core.services.activitypub.outbox import (
    _process_outbox_add,
    _process_outbox_remove,
)
from firm.core.services.activitypub.support import FirmBoxContext
from firm.core.store.memory import MemoryResourceStore
from tests.support import StubDeliveryService, make_context

PREFIX = "http://tenant1.test"
ACTOR = f"{PREFIX}/user1"
COLLECTION = f"{ACTOR}/likes"
NOTE = f"{PREFIX}/note/1"
OTHER = f"{PREFIX}/note/2"


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
        "likes": COLLECTION,
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


def _activity(name: str, activity_type: str, object_: str) -> JSONObject:
    return {
        "id": f"{PREFIX}/activity/{name}",
        "type": activity_type,
        "actor": ACTOR,
        "object": {"id": object_, "type": "Note"},
        "target": COLLECTION,
    }


async def test_add_puts_the_object_first(tenant: Tenant):
    """Newest first, like every other append path. Today this handler appends at the end."""
    await tenant.public_store.put(
        {"id": COLLECTION, "type": "OrderedCollection", "items": [OTHER], "totalItems": 1}
    )

    await _process_outbox_add(_context(tenant), _activity("add-1", "Add", NOTE))

    collection = await tenant.public_store.get(COLLECTION)
    assert collection is not None
    assert collection["items"] == [NOTE, OTHER]
    assert collection["totalItems"] == 2


async def test_add_creates_the_items_list_when_the_collection_has_none(tenant: Tenant):
    await tenant.public_store.put({"id": COLLECTION, "type": "Collection"})

    await _process_outbox_add(_context(tenant), _activity("add-2", "Add", NOTE))

    collection = await tenant.public_store.get(COLLECTION)
    assert collection is not None
    assert collection["items"] == [NOTE]
    assert collection["totalItems"] == 1


async def test_add_to_something_that_is_not_a_collection_raises(tenant: Tenant):
    await tenant.public_store.put({"id": COLLECTION, "type": "Note"})

    with pytest.raises(InvalidResourceException):
        await _process_outbox_add(_context(tenant), _activity("add-3", "Add", NOTE))


async def test_remove_drops_the_object_from_the_target_collection(tenant: Tenant):
    await tenant.public_store.put(
        {"id": COLLECTION, "type": "OrderedCollection", "items": [NOTE, OTHER], "totalItems": 2}
    )

    await _process_outbox_remove(_context(tenant), _activity("remove-1", "Remove", NOTE))

    collection = await tenant.public_store.get(COLLECTION)
    assert collection is not None
    assert collection["items"] == [OTHER]
    assert collection["totalItems"] == 1


async def test_remove_of_an_absent_object_leaves_the_collection_alone(tenant: Tenant):
    await tenant.public_store.put(
        {"id": COLLECTION, "type": "OrderedCollection", "items": [OTHER], "totalItems": 1}
    )

    await _process_outbox_remove(_context(tenant), _activity("remove-2", "Remove", NOTE))

    collection = await tenant.public_store.get(COLLECTION)
    assert collection is not None
    assert collection["items"] == [OTHER]
    assert collection["totalItems"] == 1


async def test_remove_from_a_collection_without_a_list_is_a_noop(tenant: Tenant):
    """The store owns the list; a collection with none simply has nothing to remove."""
    await tenant.public_store.put({"id": COLLECTION, "type": "Collection"})

    await _process_outbox_remove(_context(tenant), _activity("remove-3", "Remove", NOTE))

    collection = await tenant.public_store.get(COLLECTION)
    assert collection == {"id": COLLECTION, "type": "Collection"}
