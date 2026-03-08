import json
from typing import Sequence

import pytest

from firm.core.interfaces import (
    HttpException,
    Identity,
    JSONObject,
    ResourceStore,
    Tenant,
)
from firm.core.services.activitypub import ActivityPubService
from firm.core.store.memory import MemoryResourceStore
from firm.core.util import AS2_CONTENT_TYPES
from tests.support import (
    StubAuthorizationService,
    StubDeliveryService,
    StubHttpRequest,
    StubIdentity,
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
    request = StubHttpRequest("GET", "http://tenant1.test/bogus", tenant=tenant)
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 404


async def test_dereference(service: ActivityPubService, tenant: Tenant):
    resource: JSONObject = {"id": "http://tenant1.test/obj1", "type": "Object"}
    await tenant.public_store.put(resource)
    request = StubHttpRequest("GET", "http://tenant1.test/obj1", tenant=tenant)
    response = await service.process_request(request)
    assert response.status_code == 200
    assert response.media_type == "application/activity+json"
    assert response.body == json.dumps(resource).encode()


async def test_inbox_no_auth(service: ActivityPubService, tenant: Tenant):
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        headers={"Content-Type": AS2_CONTENT_TYPES[0]},
        tenant=tenant,
    )
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 403


async def test_inbox_bad_uri(service: ActivityPubService, identity):
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=identity,
        headers={"Content-Type": AS2_CONTENT_TYPES[0]},
    )
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 400


async def test_inbox_bad_type(service: ActivityPubService, identity: Identity):
    await identity.tenant.public_store.put(
        {"id": "http://tenant1.test/inbox", "type": "Collection"}
    )
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=identity,
        headers={"Content-Type": AS2_CONTENT_TYPES[0]},
    )
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 400


async def test_inbox_no_attribution(service: ActivityPubService, tenant: Tenant):
    await tenant.public_store.put(
        {
            "id": "http://tenant1.test/inbox",
            "type": "OrderedCollection",
        }
    )
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=StubIdentity(
            {
                "id": "http://tenant1.test/user1",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox2",
                "outbox": "",
                "followers": "",
                "following": "",
                "likes": "",
                # "liked": ""
            },
            tenant,
        ),
        headers={"Content-Type": AS2_CONTENT_TYPES[0]},
    )
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 400


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

    # Follow request
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=remote_identity,
        body=json.dumps(
            {
                "id": "http://remote.test/follow1",
                "type": "Follow",
                "actor": "http://remote.test/user1",
                "object": "http://tenant1.test/user2",
            }
        ).encode(),
        headers={"Content-Type": AS2_CONTENT_TYPES[0]},
    )
    response = await service.process_request(request)

    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert isinstance(inbox["orderedItems"], list)
    assert len(inbox["orderedItems"]) == 1
    assert len(await tenant.public_store.query({"type": "Follow"})) > 0
    outbox = await tenant.public_store.get("http://tenant1.test/outbox")
    assert outbox and isinstance(outbox["orderedItems"], Sequence)
    assert len(outbox["orderedItems"]) == 1
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

    # Follow request
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=remote_identity,
        body=json.dumps(
            {
                "type": "Undo",
                "actor": "http://remote.test/user1",
                "object": {
                    # The Follow activity
                    "type": "Follow",
                    "object": "http://tenant1.test/user2",
                },
            }
        ).encode(),
        headers={"Content-Type": AS2_CONTENT_TYPES[0]},
    )
    response = await service.process_request(request)
    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    followers = await tenant.public_store.get("http://tenant1.test/user2/followers")
    assert followers and isinstance(followers["items"], Sequence)
    assert followers["items"] == []
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert len(inbox["orderedItems"]) == 1
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

    # Follow request
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=remote_identity,
        body=json.dumps(
            {
                "id": "http://remote.test/follow1",
                "type": "Like",
                "actor": "http://remote.test/user1",
                # They are liking the user in this case
                "object": "http://tenant1.test/user2/note",
            }
        ).encode(),
        headers={"Content-Type": AS2_CONTENT_TYPES[0]},
    )
    response = await service.process_request(request)

    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert len(inbox["orderedItems"]) == 1
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

    # Follow request
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=remote_identity,
        body=json.dumps(
            {
                "type": "Undo",
                "actor": "http://remote.test/user1",
                "object": {
                    "type": "Like",
                    "object": "http://tenant1.test/user2/note",
                },
            }
        ).encode(),
        headers={"Content-Type": AS2_CONTENT_TYPES[0]},
    )
    response = await service.process_request(request)

    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert len(inbox["orderedItems"]) == 1
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

    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=remote_identity,
        body=json.dumps(
            {
                "id": "http://remote.test/create1",
                "type": "Create",
                "actor": "http://remote.test/user1",
                "object": {
                    "id": "http://tenant1.test/user2/document",
                    "type": "Document",
                    "content": "Some stuff...",
                },
            }
        ).encode(),
        headers={"Content-Type": AS2_CONTENT_TYPES[0]},
    )

    response = await service.process_request(request)

    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert len(inbox["orderedItems"]) == 1
    create_activity = (await tenant.public_store.query({"type": "Create"}))[0]
    assert create_activity["object"] == "http://tenant1.test/user2/document"
    document = (await tenant.public_store.query({"type": "Document"}))[0]
    assert create_activity["object"] == document["id"]
