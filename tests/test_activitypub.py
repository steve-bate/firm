import json
from typing import Sequence

import pytest

from firm.interfaces import (
    DeliveryService,
    HttpException,
    JSONObject,
    ResourceStore,
    UrlPrefix,
)
from firm.services.activitypub import ActivityPubService, ActivityPubTenant
from firm.store.memory import MemoryResourceStore
from tests.support import StubHttpRequest, StubIdentity


@pytest.fixture
def store():
    return MemoryResourceStore()
    # return ResourceStore(
    #     {
    #         "http://tenant1.test": MemoryResourcePartition(),
    #         "http://tenant2.test": MemoryResourcePartition(),
    #     }
    # )


class StubDeliveryService(DeliveryService):
    async def deliver(self, activity: JSONObject) -> None:
        pass


@pytest.fixture
def tenant1(store: ResourceStore):
    return ActivityPubTenant(
        UrlPrefix("http", "tenant1.test", None), store, StubDeliveryService()
    )


@pytest.fixture
def tenant2(store: ResourceStore):
    return ActivityPubTenant(
        UrlPrefix("http", "tenant2.test", None), store, StubDeliveryService()
    )


@pytest.fixture
def service(tenant1: ActivityPubTenant, tenant2: ActivityPubTenant):
    fedi = ActivityPubService()
    fedi.tenants[str(tenant1.prefix)] = tenant1
    fedi.tenants[str(tenant2.prefix)] = tenant2
    return fedi


async def test_dereference_unknown_resource(service: ActivityPubService):
    request = StubHttpRequest("GET", "http://tenant1.test/bogus")
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 404


async def test_dereference(service: ActivityPubService, store: ResourceStore):
    resource: JSONObject = {"id": "http://tenant1.test/obj1", "type": "Object"}
    await store.put(resource)
    request = StubHttpRequest("GET", "http://tenant1.test/obj1")
    response = await service.process_request(request)
    assert response.status_code == 200
    assert response.media_type == "application/activity+json"
    assert response.body == json.dumps(resource).encode()


async def test_inbox_no_auth(service: ActivityPubService):
    request = StubHttpRequest("POST", "http://tenant1.test/inbox")
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 403


async def test_inbox_bad_uri(service: ActivityPubService):
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=StubIdentity("http://tenant1.test/user1"),
    )
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 400


async def test_inbox_bad_type(service: ActivityPubService, store: ResourceStore):
    await store.put({"id": "http://tenant1.test/inbox", "type": "Collection"})
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=StubIdentity("http://tenant1.test/user1"),
    )
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 400


async def test_inbox_no_attribution(service: ActivityPubService, store: ResourceStore):
    await store.put(
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
            }
        ),
    )
    with pytest.raises(HttpException) as ex:
        await service.process_request(request)
        assert ex.value.status_code == 400


async def setup_resources(p: ResourceStore, resources: list[JSONObject]):
    for r in resources:
        await p.put(r)


async def test_inbox_follow(service: ActivityPubService, store: ResourceStore):
    await setup_resources(
        store,
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
        auth=StubIdentity("http://remote.test/user1"),
        body=json.dumps(
            {
                "id": "http://remote.test/follow1",
                "type": "Follow",
                "actor": "http://remote.test/user1",
                "object": "http://tenant1.test/user2",
            }
        ).encode(),
    )
    response = await service.process_request(request)

    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    inbox = await store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert isinstance(inbox["orderedItems"], list)
    assert len(inbox["orderedItems"]) == 1
    assert len(await store.query({"type": "Follow"})) > 0
    outbox = await store.get("http://tenant1.test/outbox")
    assert outbox and isinstance(outbox["orderedItems"], Sequence)
    assert len(outbox["orderedItems"]) == 1
    assert len(await store.query({"type": "Accept"})) > 0
    followers = await store.get("http://tenant1.test/user2/followers")
    assert followers and isinstance(followers["items"], Sequence)
    assert followers["items"] == ["http://remote.test/user1"]


async def test_inbox_undo_follow(service: ActivityPubService, store: ResourceStore):
    await setup_resources(
        store,
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
        ],
    )

    # Follow request
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=StubIdentity("http://remote.test/user1"),
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
    )
    response = await service.process_request(request)
    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    followers = await store.get("http://tenant1.test/user2/followers")
    assert followers and isinstance(followers["items"], Sequence)
    assert followers["items"] == []
    inbox = await store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert len(inbox["orderedItems"]) == 1
    assert len(await store.query({"type": "Undo"})) > 0


async def test_inbox_like(service: ActivityPubService, store: ResourceStore):
    await setup_resources(
        store,
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
        auth=StubIdentity("http://remote.test/user1"),
        body=json.dumps(
            {
                "id": "http://remote.test/follow1",
                "type": "Like",
                "actor": "http://remote.test/user1",
                # They are liking the user in this case
                "object": "http://tenant1.test/user2/note",
            }
        ).encode(),
    )
    response = await service.process_request(request)

    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    inbox = await store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert len(inbox["orderedItems"]) == 1
    assert len(await store.query({"type": "Like"})) > 0
    likes = await store.get("http://tenant1.test/user2/note/likes")
    assert likes and isinstance(likes["items"], Sequence)
    assert likes["items"] == ["http://remote.test/user1"]


async def test_inbox_undo_like(service: ActivityPubService, store: ResourceStore):
    await setup_resources(
        store,
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
        auth=StubIdentity("http://remote.test/user1"),
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
    )
    response = await service.process_request(request)

    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    inbox = await store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert len(inbox["orderedItems"]) == 1
    assert len(await store.query({"type": "Undo"})) > 0
    likes = await store.get("http://tenant1.test/user2/note/likes")
    assert likes and isinstance(likes["items"], Sequence)
    assert likes["items"] == []


async def test_inbox_create_object(service: ActivityPubService, store: ResourceStore):
    await setup_resources(
        store,
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

    # Follow request
    request = StubHttpRequest(
        "POST",
        "http://tenant1.test/inbox",
        auth=StubIdentity("http://remote.test/user1"),
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
    )

    response = await service.process_request(request)

    assert response.status_code == 200
    assert response.reason_phrase == "OK"
    inbox = await store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["orderedItems"], Sequence)
    assert len(inbox["orderedItems"]) == 1
    create_activity = (await store.query({"type": "Create"}))[0]
    assert create_activity["object"] == "http://tenant1.test/user2/document"
    document = (await store.query({"type": "Document"}))[0]
    assert create_activity["object"] == document["id"]
