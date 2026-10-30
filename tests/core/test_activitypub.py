import copy
from typing import Sequence, cast

import pytest

from firm.core.auth.authorization import CoreAuthorizationService
from firm.core.interfaces import FIRM_NS, Identity, JSONObject, ResourceStore, Tenant
from firm.core.remote.cache import MemoryRemoteCache
from firm.core.remote.provider import FetchPolicy, RemoteResourceProvider
from firm.core.services.activitypub.collections import dereference_collection_items
from firm.core.services.activitypub.exceptions import (
    InvalidRequestException,
    InvalidResourceTypeException,
    NotAuthorizedException,
    NotFoundException,
    ResourceOwnerException,
)
from firm.core.services.activitypub.serialization import serialize
from firm.core.services.activitypub.service import (
    ActivityPubService,
)
from firm.core.store.memory import MemoryResourceStore
from tests.support import (
    CountingStore,
    RecordingAuthorizer,
    StubAuthorizationService,
    StubDeliveryService,
    StubIdentity,
    StubTransport,
    StubUrl,
    json_response,
    make_context,
)


@pytest.fixture
def tenant(tmp_path):
    return Tenant(
        "http://tenant1.test",
        CountingStore(),
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


@pytest.fixture
def recording_service():
    delivery = StubDeliveryService()
    return ActivityPubService(StubAuthorizationService(), delivery), delivery


async def test_outbox_post_appends_and_delivers_once(
    recording_service: tuple[ActivityPubService, StubDeliveryService], tenant: Tenant
):
    service, delivery = recording_service
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox",
                "outbox": "http://tenant1.test/outbox",
            },
            {
                "id": "http://tenant1.test/outbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
        ],
    )
    activity: JSONObject = {
        "id": "http://tenant1.test/create1",
        "type": "Create",
        "actor": "http://tenant1.test/user2",
        "object": {"type": "Note", "content": "hello"},
    }

    await service.process_post(
        make_context(tenant, principal=StubIdentity("http://tenant1.test/user2", tenant)),
        StubUrl.parse("http://tenant1.test/outbox"),
        activity,
    )

    outbox = await tenant.public_store.get("http://tenant1.test/outbox")
    assert outbox is not None
    items = outbox["items"]
    assert isinstance(items, list)  # the create handler assigns the activity its own id
    assert len(items) == 1, "appended once, not twice"
    assert delivery.delivered == [items[0]], "delivered once, and the activity that was stored"


async def test_dereference_unknown_resource(service: ActivityPubService, tenant: Tenant):
    with pytest.raises(NotFoundException):
        await service.process_get(
            make_context(tenant), StubUrl.parse("http://tenant1.test/bogus"), {}
        )


async def test_dereference(service: ActivityPubService, tenant: Tenant):
    resource: JSONObject = {"id": "http://tenant1.test/obj1", "type": "Object"}
    await tenant.public_store.put(resource)
    resource = await service.process_get(
        make_context(tenant), StubUrl.parse("http://tenant1.test/obj1"), {}
    )
    assert resource == {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": "http://tenant1.test/obj1",
        "type": "Object",
    }


async def test_resource_resolves_when_the_request_has_a_query_string(
    service: ActivityPubService, tenant: Tenant
):
    await tenant.public_store.put({"id": "http://tenant1.test/obj1", "type": "Object"})

    resource = await service.process_get(
        make_context(tenant), StubUrl.parse("http://tenant1.test/obj1?page=2"), {}
    )

    assert resource["id"] == "http://tenant1.test/obj1"


async def test_tenant_root_resolves_when_the_request_has_a_query_string(
    service: ActivityPubService, tenant: Tenant
):
    await tenant.public_store.put({"id": "http://tenant1.test", "type": "Service"})

    resource = await service.process_get(
        make_context(tenant), StubUrl.parse("http://tenant1.test/?page=2"), {}
    )

    assert resource["id"] == "http://tenant1.test"


async def test_box_post_resolves_when_the_request_has_a_query_string(
    recording_service: tuple[ActivityPubService, StubDeliveryService], tenant: Tenant
):
    service, delivery = recording_service
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox",
                "outbox": "http://tenant1.test/outbox",
            },
            {
                "id": "http://tenant1.test/outbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
        ],
    )
    activity: JSONObject = {
        "id": "http://tenant1.test/create-q",
        "type": "Create",
        "actor": "http://tenant1.test/user2",
        "object": {"type": "Note", "content": "hello"},
    }

    await service.process_post(
        make_context(tenant, principal=StubIdentity("http://tenant1.test/user2", tenant)),
        StubUrl.parse("http://tenant1.test/outbox?page=2"),
        activity,
    )

    outbox = await tenant.public_store.get("http://tenant1.test/outbox")
    assert outbox is not None
    items = outbox["items"]
    assert isinstance(items, list)
    assert len(items) == 1
    assert delivery.delivered == items


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
        make_context(tenant), StubUrl.parse("http://tenant1.test/collection"), {}
    )
    assert collection == {
        "@context": "https://www.w3.org/ns/activitystreams",
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


async def test_serialize_does_not_mutate_the_collection_it_is_given(
    service: ActivityPubService, tenant: Tenant
):
    await tenant.public_store.put({"id": "http://tenant1.test/obj1", "type": "Object"})
    collection: JSONObject = {
        "id": "http://tenant1.test/collection",
        "type": "OrderedCollection",
        "items": ["http://tenant1.test/obj1"],
        "totalItems": 1,
    }
    snapshot = copy.deepcopy(collection)

    serialized = await serialize(make_context(tenant), collection)

    assert collection == snapshot
    assert serialized["orderedItems"] == [{"id": "http://tenant1.test/obj1", "type": "Object"}]
    assert "items" not in serialized


async def test_serialize_rejects_a_non_object_as_a_bad_request(tenant: Tenant):
    with pytest.raises(InvalidRequestException):
        await serialize(make_context(tenant), cast(JSONObject, []))


async def test_dereference_collection_items_rejects_non_list_items_as_a_bad_request(
    tenant: Tenant,
):
    collection: JSONObject = {
        "id": "http://tenant1.test/collection",
        "type": "Collection",
        "items": "http://tenant1.test/obj1",
    }

    with pytest.raises(InvalidRequestException):
        await dereference_collection_items(make_context(tenant), collection)


async def test_serialize_serves_a_likes_collection_without_attributed_to(tenant: Tenant):
    note_uri = "http://tenant1.test/user2/note/1"
    likes_uri = f"{note_uri}/likes"
    await tenant.public_store.put(
        {"id": likes_uri, "type": "Collection", "items": ["http://tenant1.test/user1"]}
    )
    await tenant.public_store.put({"id": note_uri, "type": "Note", "likes": likes_uri})
    activity: JSONObject = {
        "id": "http://tenant1.test/user2/activity/1",
        "type": "Create",
        "object": note_uri,
    }

    served = await serialize(make_context(tenant), activity)

    note = cast(JSONObject, served["object"])
    likes = cast(JSONObject, note["likes"])
    assert likes["id"] == likes_uri
    assert "items" not in likes, "the emptied items list is dropped"


async def test_create_object_is_resolved_through_the_context(tenant: Tenant):
    object_uri = "http://remote.test/note/42"
    activity: JSONObject = {
        "id": "http://tenant1.test/user2/activity/2",
        "type": "Create",
        "object": object_uri,
    }
    note: JSONObject = {"id": object_uri, "type": "Note", "content": "hello"}
    transport = StubTransport({object_uri: json_response(note)})
    context = make_context(tenant, remote=_provider(transport))

    served = await serialize(context, activity)

    assert served["object"] == note, "a fetched object is embedded"
    assert transport.urls == [object_uri], "the fetch is the context's, not the store's"


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
        make_context(tenant),
        StubUrl.parse("http://tenant1.test/collection"),
        {
            "filter": "$[?@.type == 'Place']",
        },
    )
    assert collection == {
        "@context": "https://www.w3.org/ns/activitystreams",
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
        make_context(tenant),
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
            make_context(tenant), StubUrl.parse("http://tenant1.test/inbox"), {"id": "TEST"}
        )


async def test_inbox_bad_uri(service: ActivityPubService, tenant: Tenant, identity: Identity):
    with pytest.raises(NotFoundException):
        await service.process_post(
            make_context(tenant, principal=identity),
            StubUrl.parse("http://tenant1.test/inbox"),
            {"id": "TEST"},
        )


async def test_inbox_bad_type(service: ActivityPubService, tenant: Tenant, identity: Identity):
    await tenant.public_store.put({"id": "http://tenant1.test/inbox", "type": "Collection"})
    with pytest.raises(InvalidResourceTypeException):
        await service.process_post(
            make_context(tenant, principal=identity),
            StubUrl.parse("http://tenant1.test/inbox"),
            {"id": "TEST"},
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
            make_context(tenant, principal=identity),
            StubUrl.parse("http://tenant1.test/inbox"),
            {"id": "TEST"},
        )


async def setup_resources(p: ResourceStore, resources: list[JSONObject]):
    for r in resources:
        await p.put(r)


async def test_inbox_reply_publishes_once(
    recording_service: tuple[ActivityPubService, StubDeliveryService], tenant: Tenant
):
    service, delivery = recording_service
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
    follow: JSONObject = {
        "id": "http://remote.test/follow1",
        "type": "Follow",
        "actor": "http://remote.test/user1",
        "object": "http://tenant1.test/user2",
    }

    await service.process_post(
        make_context(tenant, principal=StubIdentity("http://remote.test/user1", tenant)),
        StubUrl.parse("http://tenant1.test/inbox"),
        follow,
    )

    outbox = await tenant.public_store.get("http://tenant1.test/outbox")
    assert outbox is not None
    items = outbox["items"]
    assert isinstance(items, list)
    assert len(items) == 1, "the reply is published once"
    assert delivery.delivered == [items[0]], "and delivered once, from the actor's outbox"


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

    transport = StubTransport({})
    provider = _provider(transport)
    await service.process_post(
        make_context(tenant, principal=remote_identity, remote=provider),
        StubUrl.parse("http://tenant1.test/inbox"),
        resource,
    )

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert isinstance(inbox["items"], list)
    assert inbox["items"] == ["http://remote.test/follow1"]
    assert (
        await tenant.public_store.get("http://remote.test/follow1") is None
    ), "the received Follow is not a tenant document"
    assert (
        await provider.get("http://remote.test/follow1") is not None
    ), "remembered in the remote cache"
    assert transport.urls == [], "served from the cache, not refetched"
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
        "id": "http://remote.test/undo-follow1",
        "type": "Undo",
        "actor": "http://remote.test/user1",
        "object": {
            # The Follow activity
            "type": "Follow",
            "object": "http://tenant1.test/user2",
        },
    }

    await service.process_post(
        make_context(tenant, principal=remote_identity),
        StubUrl.parse("http://tenant1.test/inbox"),
        resource,
    )

    followers = await tenant.public_store.get("http://tenant1.test/user2/followers")
    assert followers and isinstance(followers["items"], Sequence)
    assert followers["items"] == []
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox is not None
    assert inbox.get("items", []) == ["http://remote.test/undo-follow1"], "listed by its id"
    assert (
        len(await tenant.public_store.query({"type": "Undo"})) == 0
    ), "a received Undo is not a tenant document"


async def _setup_follow_collections(tenant: Tenant):
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
                "id": "http://tenant1.test/outbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
            {
                "id": "http://tenant1.test/user2/following",
                "type": "Collection",
                "attributedTo": "http://tenant1.test/user2",
                "items": ["http://remote.test/user1"],
            },
        ],
    )


async def test_outbox_undo_follow_removes_the_actor_from_following(
    service: ActivityPubService, tenant: Tenant
):
    await _setup_follow_collections(tenant)
    transport = StubTransport()
    context = make_context(
        tenant,
        principal=StubIdentity("http://tenant1.test/user2", tenant),
        remote=_provider(transport),
    )

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/outbox"),
        {
            "id": "http://tenant1.test/undo-follow1",
            "type": "Undo",
            "actor": "http://tenant1.test/user2",
            "object": {"type": "Follow", "object": "http://remote.test/user1"},
        },
    )

    following = await tenant.public_store.get("http://tenant1.test/user2/following")
    assert following is not None
    assert following["items"] == [], "the unfollowed actor is gone"
    assert transport.urls == [], "unfollowing needs no remote document"


async def test_outbox_undo_follow_by_id_removes_the_actor_from_following(
    service: ActivityPubService, tenant: Tenant
):
    await _setup_follow_collections(tenant)
    await tenant.public_store.put(
        {
            "id": "http://tenant1.test/follow/1",
            "type": "Follow",
            "actor": "http://tenant1.test/user2",
            "object": "http://remote.test/user1",
        }
    )
    transport = StubTransport()
    context = make_context(
        tenant,
        principal=StubIdentity("http://tenant1.test/user2", tenant),
        remote=_provider(transport),
    )

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/outbox"),
        {
            "id": "http://tenant1.test/undo-follow2",
            "type": "Undo",
            "actor": "http://tenant1.test/user2",
            "object": "http://tenant1.test/follow/1",
        },
    )

    following = await tenant.public_store.get("http://tenant1.test/user2/following")
    assert following is not None
    assert following["items"] == [], "the undone Follow names the actor"
    assert transport.urls == []


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
    transport = StubTransport({})
    provider = _provider(transport)
    await service.process_post(
        make_context(tenant, principal=remote_identity, remote=provider),
        StubUrl.parse("http://tenant1.test/inbox"),
        resource,
    )

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert inbox["items"] == ["http://remote.test/follow1"]
    assert (
        await tenant.public_store.get("http://remote.test/follow1") is None
    ), "the received Like is not a tenant document"
    assert (
        await provider.get("http://remote.test/follow1") is not None
    ), "remembered in the remote cache"
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
        "id": "http://remote.test/undo-like1",
        "type": "Undo",
        "actor": "http://remote.test/user1",
        "object": {
            "type": "Like",
            "object": "http://tenant1.test/user2/note",
        },
    }

    await service.process_post(
        make_context(tenant, principal=remote_identity),
        StubUrl.parse("http://tenant1.test/inbox"),
        resource,
    )

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox is not None
    assert inbox.get("items", []) == ["http://remote.test/undo-like1"], "listed by its id"
    assert (
        len(await tenant.public_store.query({"type": "Undo"})) == 0
    ), "a received Undo is not a tenant document"
    likes = await tenant.public_store.get("http://tenant1.test/user2/note/likes")
    assert likes and isinstance(likes["items"], Sequence)
    assert likes["items"] == []


async def test_inbox_rejects_an_activity_without_an_id(
    service: ActivityPubService, remote_identity: StubIdentity
):
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
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2/followers",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
                "items": ["http://remote.test/user1"],
            }
        ],
    )
    resource: JSONObject = {
        "type": "Undo",
        "actor": "http://remote.test/user1",
        "object": {"type": "Follow", "object": "http://tenant1.test/user2"},
    }

    with pytest.raises(InvalidRequestException):
        await service.process_post(
            make_context(tenant, principal=remote_identity),
            StubUrl.parse("http://tenant1.test/inbox"),
            resource,
        )


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
        make_context(tenant, principal=remote_identity),
        StubUrl.parse("http://tenant1.test/inbox"),
        resource,
    )

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert inbox["items"] == ["http://remote.test/create1"], "the activity is still listed"

    assert (
        await tenant.public_store.get("http://remote.test/create1") is None
    ), "the received activity belongs to another server"
    document = (await tenant.public_store.query({"type": "Document"}))[0]
    assert document["id"] == "http://tenant1.test/user2/document", "an own-prefix object is ours"


async def test_inbox_create_is_not_written_into_the_tenant_store(
    service: ActivityPubService, remote_identity: StubIdentity
):
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
    transport = StubTransport({})
    context = make_context(tenant, principal=remote_identity, remote=_provider(transport))
    resource: JSONObject = {
        "id": "http://remote.test/create9",
        "type": "Create",
        "actor": "http://remote.test/user1",
        "object": {
            "id": "http://tenant1.test/user2/note/9",
            "type": "Note",
            "content": "hello",
        },
    }

    await service.process_post(context, StubUrl.parse("http://tenant1.test/inbox"), resource)

    assert (
        await tenant.public_store.get("http://remote.test/create9") is None
    ), "a remote activity is not ours to store"
    assert (
        await tenant.public_store.get("http://tenant1.test/user2/note/9")
    ) is not None, "an object under our prefix is ours"

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox is not None and inbox["items"] == ["http://remote.test/create9"], "still listed"

    remembered = await context.resolver.fetch("http://remote.test/create9")
    assert remembered is not None and remembered["id"] == "http://remote.test/create9"
    assert transport.urls == [], "remembered in the remote cache, not refetched"


@pytest.mark.parametrize(
    "actor_uri, authorized",
    [
        ("http://tenant1.test/user1", True),
        ("http://tenant1.test/user3", False),
    ],
)
async def test_get_actor_blocks(
    service: ActivityPubService,
    tenant: Tenant,
    actor_uri: str,
    authorized: bool,
):
    await tenant.private_store.put(
        {
            "type": FIRM_NS.Blocks.value,
            "id": "http://tenant1.test/user1/blocks",
            "attributedTo": "http://tenant1.test/user1",
            FIRM_NS.blockedActor.value: ["http://tenant1.test/user3"],
            FIRM_NS.blockedDomain.value: ["tenant1.test"],
            FIRM_NS.blockedSubnet.value: "192.168.0.0/16",
        }
    )
    await tenant.public_store.put({"id": actor_uri, "type": "Person"})

    resource = await service.process_get(
        make_context(tenant, principal=StubIdentity(actor_uri, tenant)),
        StubUrl.parse(actor_uri),
        {},
    )

    context = resource.get("@context", [])

    if authorized:
        assert isinstance(context, list)
        assert "https://purl.archive.org/socialweb/blocked" in context
        assert resource["blocks"] == ["http://tenant1.test/user3"]
    else:
        # assert "https://purl.archive.org/socialweb/blocked" not in context
        assert "blocks" not in resource


def _provider(transport: StubTransport) -> RemoteResourceProvider:
    return RemoteResourceProvider(
        MemoryRemoteCache(), transport, policy=FetchPolicy(allow_private_addresses=True)
    )


async def test_dereference_uses_the_tenant_store_for_its_own_prefix(tenant: Tenant):
    resource: JSONObject = {"id": "http://tenant1.test/obj1", "type": "Object"}
    await tenant.public_store.put(resource)

    context = make_context(tenant)

    assert await context.dereference("http://tenant1.test/obj1") == resource


async def test_dereference_fetches_for_an_unknown_prefix(tenant: Tenant):
    uri = "http://remote.test/note/1"
    transport = StubTransport({uri: json_response({"id": uri, "type": "Note"})})
    context = make_context(tenant, remote=_provider(transport))

    assert await context.dereference(uri) == {"id": uri, "type": "Note"}

    assert transport.urls == [uri]
    assert (
        cast(CountingStore, tenant.public_store).reads == []
    ), "a foreign URI never touches our store"


async def test_dereference_without_a_provider_returns_none(tenant: Tenant):
    context = make_context(tenant, remote=None)

    assert await context.dereference("http://remote.test/note/1") is None


def _two_tenants(tmp_path) -> tuple[Tenant, Tenant]:
    a = Tenant("http://a.test", CountingStore(), MemoryResourceStore(), tmp_path)
    b = Tenant("http://b.test", CountingStore(), MemoryResourceStore(), tmp_path)
    return a, b


async def test_dereference_reads_another_tenant_locally_and_authorizes(tmp_path):
    a, b = _two_tenants(tmp_path)
    document: JSONObject = {"id": "http://b.test/note/1", "type": "Note"}
    await b.public_store.put(document)
    authorizer = RecordingAuthorizer()
    transport = StubTransport({})
    context = make_context(
        a,
        tenants={"http://a.test": a, "http://b.test": b},
        principal=StubIdentity("http://a.test/user1", a),
        authorizer=authorizer,
        remote=_provider(transport),
    )

    assert await context.dereference("http://b.test/note/1") == document

    assert cast(CountingStore, b.public_store).reads == ["http://b.test/note/1"]
    assert authorizer.calls == [
        (b, context.principal, document)
    ], "the owning tenant decides, with our principal"
    assert transport.urls == [], "a local instance is never read over the network"


async def test_dereference_is_denied_by_the_owning_tenant(tmp_path):
    a, b = _two_tenants(tmp_path)
    await b.public_store.put(
        {"id": "http://b.test/note/1", "type": "Note", "to": ["http://b.test/followers"]}
    )
    context = make_context(
        a,
        tenants={"http://a.test": a, "http://b.test": b},
        principal=StubIdentity("http://a.test/user1", a),
        authorizer=RecordingAuthorizer(False, "not addressed to you"),
    )

    assert await context.dereference("http://b.test/note/1") is None


async def test_dereference_denied_with_required_raises(tmp_path):
    a, b = _two_tenants(tmp_path)
    await b.public_store.put({"id": "http://b.test/note/1", "type": "Note"})
    context = make_context(
        a,
        tenants={"http://a.test": a, "http://b.test": b},
        authorizer=RecordingAuthorizer(False, "not addressed to you"),
    )

    with pytest.raises(NotFoundException):
        await context.dereference("http://b.test/note/1", required=True)


COLLECTION_URI = "http://tenant1.test/collection"
REMOTE_ITEM = "http://remote.test/note/1"


async def _collection_with_item(tenant: Tenant, item: str) -> JSONObject:
    collection: JSONObject = {
        "id": COLLECTION_URI,
        "type": "Collection",
        "totalItems": 1,
        "items": [item],
    }
    await tenant.public_store.put(collection)
    return collection


def _public_note(uri: str) -> JSONObject:
    return {"id": uri, "type": "Note", "to": ["as:Public"], "content": "hello"}


async def test_collection_item_without_a_copy_is_dropped(tenant: Tenant):
    collection = await _collection_with_item(tenant, REMOTE_ITEM)

    served = await serialize(make_context(tenant), collection)

    assert "items" not in served and "orderedItems" not in served
    assert served["totalItems"] == 1, "the stored items are untouched"


async def test_collection_item_is_fetched_and_cached(tenant: Tenant):
    collection = await _collection_with_item(tenant, REMOTE_ITEM)
    transport = StubTransport({REMOTE_ITEM: json_response(_public_note(REMOTE_ITEM))})
    context = make_context(tenant, remote=_provider(transport))

    served = await serialize(context, collection)
    served_again = await serialize(context, collection)

    for document in (served, served_again):
        items = document.get("orderedItems") or document.get("items")
        assert items == [_public_note(REMOTE_ITEM)]
    assert transport.urls == [REMOTE_ITEM], "the second read is a cache hit"
    assert (
        await tenant.public_store.get(REMOTE_ITEM) is None
    ), "a fetched copy stays out of our store"


async def test_relationship_collection_is_served_as_uris(tenant: Tenant):
    following: JSONObject = {
        "id": "http://tenant1.test/user2/following",
        "type": "Collection",
        "attributedTo": "http://tenant1.test/user2",
        "totalItems": 1,
        "items": [REMOTE_ITEM],
    }
    transport = StubTransport()
    context = make_context(tenant, remote=_provider(transport))

    served = await serialize(context, following)

    assert served["items"] == [REMOTE_ITEM], "the item is an IRI, not an object"
    assert transport.urls == [], "a list of IRIs is served without fetching them"


async def test_inbox_collection_embeds_its_activities(tenant: Tenant, identity: StubIdentity):
    activity: JSONObject = {
        "id": "http://tenant1.test/user1/activity/1",
        "type": "Announce",
        "actor": "http://tenant1.test/user2",
        "to": [identity.uri],
        "object": REMOTE_ITEM,
    }
    await tenant.public_store.put(activity)
    inbox: JSONObject = {
        "id": "http://tenant1.test/user1/inbox",
        "type": "OrderedCollection",
        "attributedTo": identity.uri,
        "totalItems": 1,
        "items": [activity["id"]],
    }
    await tenant.public_store.put(inbox)
    context = make_context(tenant, principal=identity, authorizer=CoreAuthorizationService())

    served = await serialize(context, inbox)

    assert (served.get("orderedItems") or served.get("items")) == [activity]


async def test_failed_item_fetch_is_dropped_and_not_retried(tenant: Tenant):
    collection = await _collection_with_item(tenant, REMOTE_ITEM)
    transport = StubTransport({REMOTE_ITEM: json_response({"error": "gone"}, status_code=404)})
    context = make_context(tenant, remote=_provider(transport))

    served = await serialize(context, collection)
    await serialize(context, collection)

    assert "orderedItems" not in served and "items" not in served
    assert transport.urls == [REMOTE_ITEM], "a 404 is remembered, not retried"


async def test_non_public_remote_item_is_dropped(tenant: Tenant, identity: StubIdentity):
    collection = await _collection_with_item(tenant, REMOTE_ITEM)
    private_note: JSONObject = {
        "id": REMOTE_ITEM,
        "type": "Note",
        "to": ["http://remote.test/someone-else"],
        "content": "not for you",
    }
    transport = StubTransport({REMOTE_ITEM: json_response(private_note)})
    context = make_context(
        tenant,
        principal=identity,
        authorizer=CoreAuthorizationService(),
        remote=_provider(transport),
    )

    served = await serialize(context, collection)

    assert "orderedItems" not in served and "items" not in served
    assert transport.urls == [REMOTE_ITEM], "the fetch happens, serving does not"


async def test_collection_item_the_policy_allows_is_served(tenant: Tenant, identity: StubIdentity):
    collection = await _collection_with_item(tenant, REMOTE_ITEM)
    addressee: JSONObject = {
        "id": REMOTE_ITEM,
        "type": "Create",
        "actor": "http://remote.test/someone",
        "to": [identity.uri],
        "object": {"id": "http://remote.test/note/8", "type": "Note"},
    }
    transport = StubTransport({REMOTE_ITEM: json_response(addressee)})
    authorizer = RecordingAuthorizer()
    context = make_context(
        tenant, principal=identity, authorizer=authorizer, remote=_provider(transport)
    )

    served = await serialize(context, collection)

    assert served.get("orderedItems") == [addressee] or served.get("items") == [addressee]
    assert authorizer.calls == [(tenant, identity, addressee)], "the policy decides"


@pytest.mark.parametrize(
    "deleted",
    [
        "http://remote.test/note/7",
        {"id": "http://remote.test/note/7", "type": "Tombstone"},
    ],
    ids=["bare-uri", "tombstone"],
)
async def test_inbox_delete_invalidates_the_cached_copy(
    service: ActivityPubService, remote_identity: StubIdentity, deleted
):
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
    note_uri = "http://remote.test/note/7"
    note: JSONObject = {"id": note_uri, "type": "Note", "content": "hello"}
    transport = StubTransport({note_uri: json_response(note)})
    provider = _provider(transport)
    context = make_context(tenant, principal=remote_identity, remote=provider)

    assert await provider.get(note_uri) == note, "the document is cached"

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        {
            "id": "http://remote.test/delete7",
            "type": "Delete",
            "actor": "http://remote.test/user1",
            "object": deleted,
        },
    )

    assert await provider.get(note_uri) == note
    assert transport.urls == [note_uri, note_uri], "the delete invalidated the cached copy"


async def test_inbox_post_gives_an_unaddressed_activity_its_recipient(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox",
                "outbox": "http://tenant1.test/outbox",
            },
            {
                "id": "http://tenant1.test/inbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
        ],
    )
    provider = _provider(StubTransport({}))
    context = make_context(tenant, principal=remote_identity, remote=provider)

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        {
            "id": "http://remote.test/create9",
            "type": "Create",
            "actor": "http://remote.test/user1",
            "object": {"id": "http://remote.test/note/9", "type": "Note"},
        },
    )

    stored = await context.dereference("http://remote.test/create9")
    assert stored is not None
    assert stored["to"] == ["http://tenant1.test/user2"], "the box owner is the recipient"


async def test_inbox_post_leaves_a_public_activity_unaddressed(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/user2",
                "type": "Person",
                "inbox": "http://tenant1.test/inbox",
                "outbox": "http://tenant1.test/outbox",
            },
            {
                "id": "http://tenant1.test/inbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
        ],
    )
    provider = _provider(StubTransport({}))
    context = make_context(tenant, principal=remote_identity, remote=provider)

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        {
            "id": "http://remote.test/create10",
            "type": "Create",
            "actor": "http://remote.test/user1",
            "to": ["as:Public"],
            "object": {"id": "http://remote.test/note/10", "type": "Note"},
        },
    )

    stored = await context.dereference("http://remote.test/create10")
    assert stored is not None
    assert stored["to"] == ["as:Public"], "a public activity needs no implicit recipient"
