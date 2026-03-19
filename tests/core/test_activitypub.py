from typing import Sequence

import pytest

from firm.core.interfaces import (
    Identity,
    JSONObject,
    ResourceStore,
    Tenant,
)
from firm.core.services.activitypub.exceptions import (
    InvalidResourceTypeException,
    NotAuthorizedException,
    NotFoundException,
    ResourceOwnerException,
)
from firm.core.services.activitypub.service import (
    ActivityPubService,
)
from firm.core.store.memory import MemoryResourceStore
from tests.support import (
    StubAuthorizationService,
    StubDeliveryService,
    StubIdentity,
    StubUrl,
)


@pytest.fixture
def tenant(tmp_path):
    return Tenant(
        "http://tenant1.test",
        MemoryResourceStore(),
        MemoryResourceStore(),
        tmp_path,
    )


@pytest.fixture
def identity(tenant: Tenant):
    return StubIdentity(
        "http://tenant1.test/user1",
        tenant,
    )


@pytest.fixture
def remote_identity(tenant: Tenant):
    return StubIdentity(
        "http://remote.test/user1",
        tenant,
    )


@pytest.fixture
def service():
    fedi = ActivityPubService(
        StubAuthorizationService(),
        StubDeliveryService(),
    )
    # fedi.tenants[str(tenant1.prefix)] = tenant1
    # fedi.tenants[str(tenant2.prefix)] = tenant2
    return fedi


async def test_dereference_unknown_resource(service: ActivityPubService, tenant: Tenant):
    with pytest.raises(NotFoundException):
        await service.process_get(
            dict(), tenant, None, StubUrl.parse("http://tenant1.test/bogus"), {}
        )


async def test_dereference(service: ActivityPubService, tenant: Tenant):
    resource: JSONObject = {"id": "http://tenant1.test/obj1", "type": "Object"}
    await tenant.public_store.put(resource)
    resource = await service.process_get(
        dict(), tenant, None, StubUrl.parse("http://tenant1.test/obj1"), {}
    )
    assert resource == {"id": "http://tenant1.test/obj1", "type": "Object"}


async def test_dereference_collection(service: ActivityPubService, tenant: Tenant):
    resources: list[JSONObject] = [
        {
            "id": "http://tenant1.test/collection",
            "type": "Collection",
            "items": [
                "http://tenant1.test/obj1",
                "http://tenant1.test/obj2",
            ],
        },
        {
            "id": "http://tenant1.test/obj1",
            "type": "Object",
            "name": "foo",
        },
        {
            "id": "http://tenant1.test/obj2",
            "type": "Object",
            "name": "bar",
        },
    ]
    for r in resources:
        await tenant.public_store.put(r)
    collection = await service.process_get(
        dict(), tenant, None, StubUrl.parse("http://tenant1.test/collection"), {}
    )
    assert collection == {
        "id": "http://tenant1.test/collection",
        "type": "Collection",
        "items": [
            {
                "id": "http://tenant1.test/obj1",
                "type": "Object",
                "name": "foo",
            },
            {
                "id": "http://tenant1.test/obj2",
                "type": "Object",
                "name": "bar",
            },
        ],
    }


async def test_dereference_collection_filtered(service: ActivityPubService, tenant: Tenant):
    resources: list[JSONObject] = [
        {
            "id": "http://tenant1.test/collection",
            "type": "Collection",
            "items": [
                "http://tenant1.test/obj1",
                "http://tenant1.test/obj2",
            ],
        },
        {
            "id": "http://tenant1.test/obj1",
            "type": "Event",
            "name": "foo",
        },
        {
            "id": "http://tenant1.test/obj2",
            "type": "Place",
            "name": "bar",
        },
    ]
    for r in resources:
        await tenant.public_store.put(r)
    collection = await service.process_get(
        dict(),
        tenant,
        None,
        StubUrl.parse("http://tenant1.test/collection"),
        {
            "filter": "$[?@.type == 'Place']",
        },
    )
    assert collection == {
        "id": "http://tenant1.test/collection",
        "type": "Collection",
        "items": [
            {
                "id": "http://tenant1.test/obj2",
                "type": "Place",
                "name": "bar",
            },
        ],
    }


async def test_dereference_sharedinbox_filtered(service: ActivityPubService, tenant: Tenant):
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/shared"

    resources: list[JSONObject] = [
        {
            "id": "http://tenant1.test/obj1",
            "type": "Create",
            "to": ["as:Public"],
            "actor": "http://remote.test/user1",
            "object": "http://tenant1.test/obj1",
            "name": "foo",
        },
        {
            "id": "http://tenant1.test/obj2",
            "type": "Update",
            "to": ["as:Public"],
            "actor": "http://remote.test/user1",
            "object": "http://tenant1.test/obj1",
            "name": "bar",
        },
    ]
    for r in resources:
        await tenant.public_store.put(r)
    collection = await service.process_get(
        dict(),
        tenant,
        None,
        StubUrl.parse("http://tenant1.test/shared?offset=0"),
        {
            "filter": "$[?@.name == 'bar']",
        },
    )
    assert collection == {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": "http://tenant1.test/shared?offset=0",
        "type": "CollectionPage",
        "totalItems": 1,
        "items": [
            {
                "id": "http://tenant1.test/obj2",
                "type": "Update",
                "to": ["as:Public"],
                "actor": "http://remote.test/user1",
                "object": "http://tenant1.test/obj1",
                "name": "bar",
            },
        ],
    }


async def test_inbox_no_auth(service: ActivityPubService, tenant: Tenant):
    with pytest.raises(NotAuthorizedException):
        await service.process_post(
            dict(), tenant, None, StubUrl.parse("http://tenant1.test/inbox"), {"id": "TEST"}
        )


async def test_inbox_bad_uri(service: ActivityPubService, tenant: Tenant, identity: Identity):
    with pytest.raises(NotFoundException):
        await service.process_post(
            dict(), tenant, identity, StubUrl.parse("http://tenant1.test/inbox"), {"id": "TEST"}
        )


async def test_inbox_bad_type(service: ActivityPubService, tenant: Tenant, identity: Identity):
    await tenant.public_store.put({"id": "http://tenant1.test/inbox", "type": "Collection"})
    with pytest.raises(InvalidResourceTypeException):
        await service.process_post(
            dict(), tenant, identity, StubUrl.parse("http://tenant1.test/inbox"), {"id": "TEST"}
        )


async def test_inbox_no_attribution(
    service: ActivityPubService, tenant: Tenant, identity: Identity
):
    await tenant.public_store.put(
        {
            "id": "http://tenant1.test/inbox",
            "type": "OrderedCollection",
        }
    )
    with pytest.raises(ResourceOwnerException):
        await service.process_post(
            dict(), tenant, identity, StubUrl.parse("http://tenant1.test/inbox"), {"id": "TEST"}
        )


async def setup_resources(p: ResourceStore, resources: list[JSONObject]):
    for r in resources:
        await p.put(r)


async def test_inbox_follow(service: ActivityPubService, remote_identity: StubIdentity):
    tenant = remote_identity.tenant
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox",
                "outbox": "http://tenant1.test/outbox",
                "followers": "http://tenant1.test/user2/followers",
                "likes": "http://tenant1.test/user2/likes",
            },
            {
                "id": "http://tenant1.test/inbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
            {
                "id": "http://tenant1.test/outbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
            {
                "id": "http://tenant1.test/user2/followers",
                "type": "Collection",
                "attributedTo": "http://tenant1.test/user2",
            },
        ],
    )

    resource: JSONObject = {
        "id": "http://remote.test/follow1",
        "type": "Follow",
        "actor": "http://remote.test/user1",
        "object": "http://tenant1.test/user2",
    }

    await service.process_post(
        dict(), tenant, remote_identity, StubUrl.parse("http://tenant1.test/inbox"), resource
    )

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert isinstance(inbox["items"], list)
    assert len(inbox["items"]) == 1
    assert len(await tenant.public_store.query({"type": "Follow"})) > 0
    outbox = await tenant.public_store.get("http://tenant1.test/outbox")
    assert outbox and isinstance(outbox["items"], Sequence)
    assert len(outbox["items"]) == 1
    assert len(await tenant.public_store.query({"type": "Accept"})) > 0
    followers = await tenant.public_store.get("http://tenant1.test/user2/followers")
    assert followers and isinstance(followers["items"], Sequence)
    assert followers["items"] == ["http://remote.test/user1"]


async def test_inbox_undo_follow(service: ActivityPubService, remote_identity: StubIdentity):
    tenant = remote_identity.tenant
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox",
                "outbox": "http://tenant1.test/outbox",
                "followers": "http://tenant1.test/user2/followers",
                "following": "http://tenant1.test/user2/following",
            },
            {
                "id": "http://tenant1.test/inbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
            {
                "id": "http://tenant1.test/outbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
            {
                "id": "http://tenant1.test/user2/followers",
                "type": "Collection",
                "attributedTo": "http://tenant1.test/user2",
                "items": ["http://remote.test/user1"],
            },
            {
                "id": "http://tenant1.test/user2/following",
                "type": "Collection",
                "attributedTo": "http://tenant1.test/user2",
                "items": ["http://remote.test/user2"],
            },
        ],
    )

    resource: JSONObject = {
        "type": "Undo",
        "actor": "http://remote.test/user1",
        "object": {
            # The Follow activity
            "type": "Follow",
            "object": "http://tenant1.test/user2",
        },
    }

    await service.process_post(
        dict(), tenant, remote_identity, StubUrl.parse("http://tenant1.test/inbox"), resource
    )

    followers = await tenant.public_store.get("http://tenant1.test/user2/followers")
    assert followers and isinstance(followers["items"], Sequence)
    assert followers["items"] == []
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert len(inbox["items"]) == 1
    assert len(await tenant.public_store.query({"type": "Undo"})) > 0


async def test_inbox_like(service: ActivityPubService, remote_identity: StubIdentity):
    tenant = remote_identity.tenant
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox",
                "outbox": "http://tenant1.test/outbox",
                "followers": "http://tenant1.test/user2/followers",
                "likes": "http://tenant1.test/user2/likes",
            },
            {
                "id": "http://tenant1.test/inbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
            {
                "id": "http://tenant1.test/user2/note",
                "type": "Note",
                "likes": "http://tenant1.test/user2/note/likes",
            },
            {
                "id": "http://tenant1.test/user2/note/likes",
                "type": "Collection",
                "attributedTo": "http://tenant1.test/user2",
            },
        ],
    )

    resource: JSONObject = {
        "id": "http://remote.test/follow1",
        "type": "Like",
        "actor": "http://remote.test/user1",
        # They are liking the user in this case
        "object": "http://tenant1.test/user2/note",
    }
    await service.process_post(
        dict(), tenant, remote_identity, StubUrl.parse("http://tenant1.test/inbox"), resource
    )

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert len(inbox["items"]) == 1
    assert len(await tenant.public_store.query({"type": "Like"})) > 0
    likes = await tenant.public_store.get("http://tenant1.test/user2/note/likes")
    assert likes and isinstance(likes["items"], Sequence)
    assert likes["items"] == ["http://remote.test/user1"]


async def test_inbox_undo_like(service: ActivityPubService, remote_identity: StubIdentity):
    tenant = remote_identity.tenant
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox",
                "outbox": "http://tenant1.test/outbox",
                "followers": "http://tenant1.test/user2/followers",
                "likes": "http://tenant1.test/user2/likes",
            },
            {
                "id": "http://tenant1.test/inbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
            {
                "id": "http://tenant1.test/user2/note",
                "type": "Note",
                "likes": "http://tenant1.test/user2/note/likes",
            },
            {
                "id": "http://tenant1.test/user2/note/likes",
                "type": "Collection",
                "attributedTo": "http://tenant1.test/user2",
                "items": ["http://remote.test/user1"],
            },
        ],
    )

    resource: JSONObject = {
        "type": "Undo",
        "actor": "http://remote.test/user1",
        "object": {
            "type": "Like",
            "object": "http://tenant1.test/user2/note",
        },
    }

    await service.process_post(
        dict(), tenant, remote_identity, StubUrl.parse("http://tenant1.test/inbox"), resource
    )

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert len(inbox["items"]) == 1
    assert len(await tenant.public_store.query({"type": "Undo"})) > 0
    likes = await tenant.public_store.get("http://tenant1.test/user2/note/likes")
    assert likes and isinstance(likes["items"], Sequence)
    assert likes["items"] == []


async def test_inbox_create_object(service: ActivityPubService, remote_identity: StubIdentity):
    tenant = remote_identity.tenant
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox",
                "outbox": "http://tenant1.test/outbox",
                "followers": "http://tenant1.test/user2/followers",
            },
            {
                "id": "http://tenant1.test/inbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
        ],
    )

    resource: JSONObject = {
        "id": "http://remote.test/create1",
        "type": "Create",
        "actor": "http://remote.test/user1",
        "object": {
            "id": "http://tenant1.test/user2/document",
            "type": "Document",
            "content": "Some stuff...",
        },
    }
    await service.process_post(
        dict(), tenant, remote_identity, StubUrl.parse("http://tenant1.test/inbox"), resource
    )

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert len(inbox["items"]) == 1
    create_activity = (await tenant.public_store.query({"type": "Create"}))[0]
    assert create_activity["object"] == "http://tenant1.test/user2/document"
    document = (await tenant.public_store.query({"type": "Document"}))[0]
    assert create_activity["object"] == document["id"]
