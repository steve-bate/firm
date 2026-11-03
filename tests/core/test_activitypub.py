import copy
import logging
from typing import Sequence, cast

import pytest

from firm.core.auth.authorization import CoreAuthorizationService
from firm.core.interfaces import (
    FIRM_NS,
    JSON,
    MAX_COLLECTION_PAGE_SIZE,
    APActor,
    DeliveryFailure,
    Identity,
    JSONObject,
    NoOpValidator,
    ResourceStore,
    Tenant,
)
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
from firm.core.services.activitypub.publish import outbox_send
from firm.core.services.activitypub.serialization import serialize
from firm.core.services.activitypub.service import (
    ActivityPubService,
    post_to_inbox,
)
from firm.core.store.file import FileResourceStore
from firm.core.store.memory import MemoryResourceStore
from firm.core.util import get_list
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
        StubUrl.parse("http://tenant1.test/collection?filter=%40.type%20%3D%3D%20%27Place%27"),
        {
            "filter": "$[?@.type == 'Place']",
        },
    )
    assert collection["type"] == "CollectionPage"
    assert collection["partOf"] == "http://tenant1.test/collection"
    assert collection["startIndex"] == 0
    assert collection["totalItems"] == 2
    served = cast(list[JSONObject], collection["items"])
    assert [item["id"] for item in served] == ["http://tenant1.test/obj2"]
    assert "next" not in collection


async def test_a_filtered_page_with_no_matches_still_says_where_to_continue(
    service: ActivityPubService, tenant: Tenant
):
    # More candidates than the scan budget (MAX_COLLECTION_PAGE_SIZE), and no matches: the
    # page comes back empty but the walk is unfinished, so it must carry `next`.
    count = MAX_COLLECTION_PAGE_SIZE + 20
    items = [f"http://tenant1.test/obj{index}" for index in range(count)]
    resources: list[JSONObject] = [
        {"id": "http://tenant1.test/collection", "type": "Collection", "items": items}
    ]
    resources.extend(
        {"id": uri, "type": "Note", "name": f"n{index}"} for index, uri in enumerate(items)
    )
    await setup_resources(tenant.public_store, resources)

    collection = await service.process_get(
        make_context(tenant),
        StubUrl.parse("http://tenant1.test/collection?filter=%40.type%20%3D%3D%20%27Nope%27"),
        {"filter": "$[?@.type == 'Nope']"},
    )

    assert collection.get("items", []) == []
    assert collection["startIndex"] == 0
    assert collection["totalItems"] == count
    next_link = str(collection["next"])
    assert "cursor=" in next_link
    assert "filter=" in next_link


async def test_a_projected_filter_serves_the_selected_values(
    service: ActivityPubService, tenant: Tenant
):
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": "http://tenant1.test/collection",
                "type": "Collection",
                "items": ["http://tenant1.test/obj1", "http://tenant1.test/obj2"],
            },
            {"id": "http://tenant1.test/obj1", "type": "Note", "name": "n1"},
            {"id": "http://tenant1.test/obj2", "type": "Place", "name": "n2"},
        ],
    )

    collection = await service.process_get(
        make_context(tenant),
        StubUrl.parse(
            "http://tenant1.test/collection?filter=%5B%3F%40.type%20%3D%3D%20%27Note%27%5D.id"
        ),
        {"filter": "[?@.type == 'Note'].id"},
    )

    assert collection["items"] == ["http://tenant1.test/obj1"]


async def test_a_filter_applies_to_an_ordered_collection(
    service: ActivityPubService, tenant: Tenant
):
    """A filtered read of an OrderedCollection must honour the filter.

    The matches are stored under "items" and serialised as "orderedItems". When the
    branch wrote the type's wire key instead, the reader missed the matches and every
    item was served.
    """
    resources: list[JSONObject] = [
        {
            "id": "http://tenant1.test/collection",
            "type": "OrderedCollection",
            "orderedItems": [
                "http://tenant1.test/obj1",
                "http://tenant1.test/obj2",
            ],
            "totalItems": 2,
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
        StubUrl.parse("http://tenant1.test/collection?filter=%40.type%20%3D%3D%20%27Place%27"),
        {
            "filter": "@.type == 'Place'",
        },
    )
    served = cast(list[JSONObject], collection["orderedItems"])
    assert [item["id"] for item in served] == ["http://tenant1.test/obj2"]


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


REMOTE_UPDATE_ID = "http://remote.test/update8"
REMOTE_UPDATE_ACTOR = "http://remote.test/user1"
REMOTE_UPDATED_OBJECT = "http://remote.test/notes/8"
HELD_OBJECT: JSONObject = {
    "id": REMOTE_UPDATED_OBJECT,
    "type": "Note",
    "name": "test note",
    "content": "original content",
    "attributedTo": REMOTE_UPDATE_ACTOR,
    "updated": "2026-11-01T10:00:00Z",
}


async def _seed_inbox_owner(tenant: Tenant) -> None:
    """A local actor with an inbox, which is all an inbox POST needs."""
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


def _update(object_document: JSON, **overrides) -> JSONObject:
    activity: JSONObject = {
        "id": REMOTE_UPDATE_ID,
        "type": "Update",
        "actor": REMOTE_UPDATE_ACTOR,
        "object": object_document,
    }
    activity.update(overrides)
    return activity


async def test_inbox_update_replaces_the_held_object_whole(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    transport = StubTransport({})
    provider = _provider(transport)
    await provider.remember(copy.deepcopy(HELD_OBJECT))
    context = make_context(tenant, principal=remote_identity, remote=provider)
    replacement: JSONObject = {
        "id": REMOTE_UPDATED_OBJECT,
        "type": "Note",
        "summary": "test summary",
        "content": "modified content",
        "attributedTo": REMOTE_UPDATE_ACTOR,
        "updated": "2026-11-02T10:00:00Z",
    }

    await service.process_post(
        context, StubUrl.parse("http://tenant1.test/inbox"), _update(replacement)
    )

    served = await context.dereference(REMOTE_UPDATED_OBJECT)
    assert served is not None
    assert served["content"] == "modified content"
    assert served["summary"] == "test summary", "the supplied object is stored"
    assert "name" not in served, "a complete replacement (AP 7.3), not a merge (AP 6.3.1)"
    assert transport.urls == [], "served at its own id from what the Update supplied"


async def test_inbox_update_for_an_object_we_do_not_hold_is_inserted(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    transport = StubTransport({})
    provider = _provider(transport)
    context = make_context(tenant, principal=remote_identity, remote=provider)
    supplied: JSONObject = {
        "id": REMOTE_UPDATED_OBJECT,
        "type": "Note",
        "content": "modified content",
        "updated": "2026-11-02T10:00:00Z",
    }

    await service.process_post(
        context, StubUrl.parse("http://tenant1.test/inbox"), _update(supplied)
    )

    assert await provider.peek(REMOTE_UPDATED_OBJECT) == supplied, "the update is kept"
    assert transport.urls == [], "an insert, not a fetch to compare against"


async def test_inbox_update_without_an_embedded_object_is_a_bad_request(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    transport = StubTransport({})
    provider = _provider(transport)
    context = make_context(tenant, principal=remote_identity, remote=provider)

    with pytest.raises(InvalidRequestException):
        await service.process_post(
            context,
            StubUrl.parse("http://tenant1.test/inbox"),
            _update(REMOTE_UPDATED_OBJECT),
        )

    assert await provider.peek(REMOTE_UPDATED_OBJECT) is None, "a reference is not a replacement"


async def test_inbox_update_from_another_origin_is_refused(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    transport = StubTransport({})
    provider = _provider(transport)
    await provider.remember(copy.deepcopy(HELD_OBJECT))
    elsewhere = StubIdentity("http://other.test/user1", tenant)
    context = make_context(tenant, principal=elsewhere, remote=provider)

    with pytest.raises(NotAuthorizedException):
        await service.process_post(
            context,
            StubUrl.parse("http://tenant1.test/inbox"),
            _update(
                {"id": REMOTE_UPDATED_OBJECT, "type": "Note", "content": "forged"},
                actor="http://other.test/user1",
            ),
        )

    assert await provider.peek(REMOTE_UPDATED_OBJECT) == HELD_OBJECT, "the copy is untouched"
    assert transport.urls == []


async def test_inbox_update_from_an_actor_other_than_the_signer_is_refused(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    transport = StubTransport({})
    provider = _provider(transport)
    await provider.remember(copy.deepcopy(HELD_OBJECT))
    context = make_context(tenant, principal=remote_identity, remote=provider)

    with pytest.raises(NotAuthorizedException):
        await service.process_post(
            context,
            StubUrl.parse("http://tenant1.test/inbox"),
            _update(
                {"id": REMOTE_UPDATED_OBJECT, "type": "Note", "content": "forged"},
                actor="http://remote.test/user3",
            ),
        )

    assert await provider.peek(REMOTE_UPDATED_OBJECT) == HELD_OBJECT


async def test_inbox_update_of_a_local_object_is_refused(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    local_uri = "http://tenant1.test/user2/notes/1"
    document: JSONObject = {"id": local_uri, "type": "Note", "content": "ours"}
    await tenant.public_store.put(document)
    transport = StubTransport({})
    context = make_context(tenant, principal=remote_identity, remote=_provider(transport))

    with pytest.raises(NotAuthorizedException):
        await service.process_post(
            context,
            StubUrl.parse("http://tenant1.test/inbox"),
            _update({"id": local_uri, "type": "Note", "content": "theirs"}),
        )

    assert await tenant.public_store.get(local_uri) == document, "another server never rewrites it"


async def test_inbox_update_person_replaces_the_actor_document(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    transport = StubTransport({})
    provider = _provider(transport)
    await provider.remember(
        {
            "id": REMOTE_UPDATE_ACTOR,
            "type": "Person",
            "name": "Old name",
            "publicKey": {
                "id": f"{REMOTE_UPDATE_ACTOR}#main-key",
                "owner": REMOTE_UPDATE_ACTOR,
                "publicKeyPem": "old-key",
            },
        }
    )
    context = make_context(tenant, principal=remote_identity, remote=provider)
    renamed: JSONObject = {
        "id": REMOTE_UPDATE_ACTOR,
        "type": "Person",
        "name": "New name",
        "updated": "2026-11-02T10:00:00Z",
        "publicKey": {
            "id": f"{REMOTE_UPDATE_ACTOR}#main-key",
            "owner": REMOTE_UPDATE_ACTOR,
            "publicKeyPem": "new-key",
        },
    }

    await service.process_post(
        context, StubUrl.parse("http://tenant1.test/inbox"), _update(renamed)
    )

    served = await provider.peek(REMOTE_UPDATE_ACTOR)
    assert served is not None
    assert served["name"] == "New name"
    public_key = cast(JSONObject, served["publicKey"])
    assert public_key["publicKeyPem"] == "new-key", "the owner may rekey itself"


async def test_inbox_update_person_from_another_actor_on_the_same_origin_is_refused(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    transport = StubTransport({})
    provider = _provider(transport)
    other_actor = "http://remote.test/user9"
    held: JSONObject = {
        "id": other_actor,
        "type": "Person",
        "publicKey": {
            "id": f"{other_actor}#main-key",
            "owner": other_actor,
            "publicKeyPem": "their-key",
        },
    }
    await provider.remember(copy.deepcopy(held))
    context = make_context(tenant, principal=remote_identity, remote=provider)

    with pytest.raises(NotAuthorizedException):
        await service.process_post(
            context,
            StubUrl.parse("http://tenant1.test/inbox"),
            _update(
                {
                    "id": other_actor,
                    "type": "Person",
                    "publicKey": {
                        "id": f"{other_actor}#main-key",
                        "owner": other_actor,
                        "publicKeyPem": "swapped-key",
                    },
                },
            ),
        )

    assert await provider.peek(other_actor) == held, "a signer cannot swap another actor's key"


async def test_inbox_update_older_than_the_copy_we_hold_is_ignored(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    held = copy.deepcopy(HELD_OBJECT)
    held["updated"] = "2026-11-02T10:00:00Z"
    transport = StubTransport({})
    provider = _provider(transport)
    await provider.remember(held)
    context = make_context(tenant, principal=remote_identity, remote=provider)

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        _update(
            {
                "id": REMOTE_UPDATED_OBJECT,
                "type": "Note",
                "content": "stale content",
                "updated": "2026-11-01T10:00:00Z",
            }
        ),
    )

    assert await provider.peek(REMOTE_UPDATED_OBJECT) == held, "an older copy does not roll back"


async def test_inbox_update_arriving_twice_leaves_one_listing_and_one_copy(
    service: ActivityPubService, remote_identity: StubIdentity
):
    tenant = remote_identity.tenant
    await _seed_inbox_owner(tenant)
    transport = StubTransport({})
    provider = _provider(transport)
    context = make_context(tenant, principal=remote_identity, remote=provider)
    supplied: JSONObject = {
        "id": REMOTE_UPDATED_OBJECT,
        "type": "Note",
        "content": "modified content",
        "updated": "2026-11-02T10:00:00Z",
    }
    activity = _update(supplied)

    await service.process_post(context, StubUrl.parse("http://tenant1.test/inbox"), activity)
    await service.process_post(
        context, StubUrl.parse("http://tenant1.test/inbox"), copy.deepcopy(activity)
    )

    assert await provider.peek(REMOTE_UPDATED_OBJECT) == supplied
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert inbox["items"] == [REMOTE_UPDATE_ID], "a redelivery adds no second listing"


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


async def test_inbox_unhandled_type_is_kept_without_effects(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """#36: a type with no inbox branch is accepted and recorded, and nothing is applied."""
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
            {
                "id": "http://tenant1.test/outbox",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
            },
            {
                "id": "http://tenant1.test/user2/followers",
                "type": "Collection",
                "attributedTo": "http://tenant1.test/user2",
                "items": [],
            },
        ],
    )
    delivery = StubDeliveryService()
    service = ActivityPubService(StubAuthorizationService(), delivery)
    provider = _provider(StubTransport({}))
    context = make_context(tenant, principal=remote_identity, remote=provider)
    flag: JSONObject = {
        "id": "http://remote.test/flag1",
        "type": "Flag",
        "actor": "http://remote.test/user1",
        "object": ["http://tenant1.test/user2"],
        "content": "spam",
    }

    accepted = await service.process_post(context, StubUrl.parse("http://tenant1.test/inbox"), flag)

    assert accepted is None, "a None return is the route's 200, not an escaping exception"
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert inbox["items"] == ["http://remote.test/flag1"], "the activity is recorded"
    assert (
        await provider.get("http://remote.test/flag1") is not None
    ), "and its document is kept in the remote cache"
    assert delivery.activities == [], "no handler ran, so nothing was applied or sent"
    assert await tenant.public_store.query({"type": "Accept"}) == []
    followers = await tenant.public_store.get("http://tenant1.test/user2/followers")
    assert followers is not None
    assert followers["items"] == []


async def test_inbox_unhandled_type_is_accepted_again_on_retry(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """#36: the status is stable across retries and the inbox keeps one item."""
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
    flag: JSONObject = {
        "id": "http://remote.test/flag2",
        "type": "Flag",
        "actor": "http://remote.test/user1",
        "object": ["http://tenant1.test/user2"],
    }

    first = await service.process_post(
        context, StubUrl.parse("http://tenant1.test/inbox"), copy.deepcopy(flag)
    )
    second = await service.process_post(
        context, StubUrl.parse("http://tenant1.test/inbox"), copy.deepcopy(flag)
    )

    assert first is None and second is None, "the same type answers 200 both times"
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert inbox["items"] == ["http://remote.test/flag2"], "a retry adds no second item"


async def test_outbox_send_logs_a_failed_delivery(tenant, caplog):
    outbox = f"{tenant.prefix}/outbox"
    await tenant.public_store.put({"id": outbox, "type": "OrderedCollection", "items": []})
    activity: JSONObject = {
        "id": f"{tenant.prefix}/activities/1",
        "type": "Create",
        "actor": f"{tenant.prefix}/user1",
    }
    delivery = StubDeliveryService()
    delivery.failures = [DeliveryFailure("https://remote.test/inbox", 3, "HTTP 503")]

    with caplog.at_level(logging.ERROR, logger="firm.core.services.activitypub.publish"):
        await outbox_send(tenant, {str(tenant.prefix): tenant}, activity, outbox, delivery)

    assert "https://remote.test/inbox" in caplog.text
    assert "HTTP 503" in caplog.text


REMOTE_REPLY_ID = "http://remote.test/activities/1"
REMOTE_AUTHOR = "http://remote.test/user9"
REMOTE_NOTE = "http://remote.test/notes/1"


async def _box_and_followers(tenant) -> tuple[JSONObject, str, str]:
    """An owner with an inbox, a second local follower, and a note of ours."""
    owner = f"{tenant.prefix}/user1"
    other = f"{tenant.prefix}/user2"
    followers = f"{owner}/followers"
    note = f"{tenant.prefix}/notes/1"
    await tenant.public_store.put(
        {"id": owner, "type": "Person", "inbox": f"{owner}/inbox", "followers": followers}
    )
    await tenant.public_store.put(
        {"id": f"{owner}/inbox", "type": "OrderedCollection", "attributedTo": owner, "items": []}
    )
    await tenant.public_store.put({"id": other, "type": "Person", "inbox": f"{other}/inbox"})
    await tenant.public_store.put(
        {"id": f"{other}/inbox", "type": "OrderedCollection", "attributedTo": other, "items": []}
    )
    await tenant.public_store.put(
        {"id": followers, "type": "Collection", "attributedTo": owner, "items": [other]}
    )
    await tenant.public_store.put({"id": note, "type": "Note", "attributedTo": owner})
    box_owner = await tenant.public_store.get(owner)
    return box_owner, followers, note


def _reply(followers: str, note: str, **overrides) -> JSONObject:
    activity: JSONObject = {
        "id": REMOTE_REPLY_ID,
        "type": "Create",
        "actor": REMOTE_AUTHOR,
        "to": [followers],
        "cc": [],
        "inReplyTo": note,
        "object": {"id": REMOTE_NOTE, "type": "Note", "inReplyTo": note},
    }
    activity.update(overrides)
    return activity


async def _arrive(tenant, box_owner: JSONObject, activity: JSONObject, delivery) -> None:
    context = make_context(tenant, principal=StubIdentity(REMOTE_AUTHOR, tenant))
    await post_to_inbox(
        context,
        cast(APActor, box_owner),
        activity,
        box_uri=str(box_owner["inbox"]),
        validator=NoOpValidator(),
        delivery_service=delivery,
    )


async def test_a_reply_addressed_to_our_followers_is_forwarded_to_them(tenant):
    box_owner, followers, note = await _box_and_followers(tenant)
    delivery = StubDeliveryService()

    await _arrive(tenant, box_owner, _reply(followers, note), delivery)

    assert delivery.delivered == [REMOTE_REPLY_ID]
    assert delivery.local_only == [True], "forwarding reaches our addressees, not the peer's"


async def test_an_activity_seen_before_is_not_forwarded_again(tenant):
    box_owner, followers, note = await _box_and_followers(tenant)
    delivery = StubDeliveryService()
    activity = _reply(followers, note)

    await _arrive(tenant, box_owner, copy.deepcopy(activity), delivery)
    await _arrive(tenant, box_owner, copy.deepcopy(activity), delivery)

    assert delivery.delivered == [REMOTE_REPLY_ID], "the second arrival is not new"


async def test_an_activity_about_nothing_of_ours_is_not_forwarded(tenant):
    box_owner, followers, _note = await _box_and_followers(tenant)
    delivery = StubDeliveryService()

    await _arrive(
        tenant,
        box_owner,
        _reply(followers, "http://third.test/notes/1"),
        delivery,
    )

    assert delivery.delivered == []


async def test_forwarding_does_not_pick_up_new_addressees(tenant):
    box_owner, followers, note = await _box_and_followers(tenant)
    delivery = StubDeliveryService()

    await _arrive(tenant, box_owner, _reply(followers, note), delivery)

    forwarded = delivery.activities[0]
    # The original addressees, plus the box owner the inbox path already added.
    assert set(forwarded["to"]) == {followers, box_owner["id"]}
    assert forwarded["cc"] == []
    assert f"{tenant.prefix}/user2" not in forwarded["to"], "the collection was not expanded"


async def test_an_activity_of_ours_is_not_forwarded(tenant):
    box_owner, followers, note = await _box_and_followers(tenant)
    delivery = StubDeliveryService()
    activity = _reply(followers, note, actor=f"{tenant.prefix}/user1")

    await _arrive(tenant, box_owner, activity, delivery)

    assert delivery.delivered == [], "our own delivery already addressed our addressees"


async def test_a_migrated_collection_is_served_with_its_items(tmp_path):
    """The item list lives in the store now, so serving one means walking its pages."""
    prefix = "http://tenant1.test"
    store = FileResourceStore(tmp_path, "public")
    tenant = Tenant(prefix, store, MemoryResourceStore(), tmp_path)
    collection_uri = f"{prefix}/user1/likes"
    item_uri = f"{prefix}/note/1"
    await store.put({"id": item_uri, "type": "Note", "attributedTo": f"{prefix}/user1"})
    await store.put(
        {
            "id": collection_uri,
            "type": "OrderedCollection",
            "attributedTo": f"{prefix}/user1",
            "items": [item_uri],
            "totalItems": 1,
        }
    )

    newer_uri = f"{prefix}/note/2"
    await store.put({"id": newer_uri, "type": "Note", "attributedTo": f"{prefix}/user1"})
    await store.insert_item(collection_uri, newer_uri)

    served = await serialize(make_context(tenant), await store.get(collection_uri))

    assert [item["id"] for item in served["orderedItems"]] == [newer_uri, item_uri]
    assert "items" not in await store.get(collection_uri), "the array is gone from the document"


async def test_serialization_uses_the_wire_item_key(service: ActivityPubService, tenant: Tenant):
    """An OrderedCollection is served as orderedItems; a Collection as items."""
    await tenant.public_store.put({"id": "http://tenant1.test/obj", "type": "Note"})
    documents: list[JSONObject] = [
        {
            "id": "http://tenant1.test/ordered",
            "type": "OrderedCollection",
            "items": ["http://tenant1.test/obj"],
        },
        {
            "id": "http://tenant1.test/plain",
            "type": "Collection",
            "items": ["http://tenant1.test/obj"],
        },
    ]
    for document in documents:
        await tenant.public_store.put(document)
    ordered = await service.process_get(
        make_context(tenant), StubUrl.parse("http://tenant1.test/ordered"), {}
    )
    plain = await service.process_get(
        make_context(tenant), StubUrl.parse("http://tenant1.test/plain"), {}
    )
    ordered_items = cast(list[JSONObject], ordered["orderedItems"])
    plain_items = cast(list[JSONObject], plain["items"])
    assert [item["id"] for item in ordered_items] == ["http://tenant1.test/obj"]
    assert [item["id"] for item in plain_items] == ["http://tenant1.test/obj"]


async def test_a_filtered_page_fetches_no_more_than_its_scan_budget(tenant: Tenant):
    """#25: one filtered request resolves at most its scan budget of items."""
    remote = "http://remote.test"
    uris = [f"{remote}/note/{index}" for index in range(10)]
    collection_uri = "http://tenant1.test/collection"
    await tenant.public_store.put(
        {"id": collection_uri, "type": "OrderedCollection", "items": uris, "totalItems": len(uris)}
    )
    transport = StubTransport({uri: json_response({"id": uri, "type": "Note"}) for uri in uris})
    provider = RemoteResourceProvider(
        MemoryRemoteCache(), transport, policy=FetchPolicy(allow_private_addresses=True)
    )
    context = make_context(tenant, remote=provider)

    page = await tenant.public_store.filter_items(
        collection_uri,
        "@.type == 'Note'",
        limit=5,
        max_scan=3,
        resolve=lambda uri: context.dereference(uri),
    )

    assert page.scanned == 3
    assert len(transport.requests) == 3
    assert page.cursor is not None


async def test_the_scan_budget_is_the_same_without_a_principal(tenant: Tenant):
    uris = [f"http://remote.test/note/{index}" for index in range(10)]
    collection_uri = "http://tenant1.test/two/collection"
    await tenant.public_store.put(
        {"id": collection_uri, "type": "OrderedCollection", "items": uris, "totalItems": 10}
    )

    async def resolve(uri: str) -> JSONObject | None:
        return {"id": uri, "type": "Note"}

    anonymous = await tenant.public_store.filter_items(
        collection_uri, "@.type == 'Note'", limit=5, max_scan=4, resolve=resolve
    )

    assert anonymous.scanned == 4
    assert len(anonymous.entries) == 4


async def test_a_received_activity_is_stored_without_firm_properties(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """#28: a document we keep carries no ``firm:`` key the sender chose to send."""
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
    context = make_context(tenant, principal=remote_identity, remote=_provider(StubTransport({})))
    toot_context: JSONObject = {"toot": "http://joinmastodon.org/ns#"}

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        {
            "id": "http://tenant1.test/activities/1",
            "type": "Create",
            "actor": "http://tenant1.test/user2",
            "@context": ["https://www.w3.org/ns/activitystreams", toot_context],
            "to": ["http://tenant1.test/user2"],
            "cc": [],
            FIRM_NS.role.value: "admin",
            "toot:discoverable": True,
            "object": {
                "id": "http://tenant1.test/user2/note/1",
                "type": "Note",
                "_misskey_quote": "http://remote.test/notes/1",
                FIRM_NS.privateKey.value: "leak",
            },
        },
    )

    stored = await tenant.public_store.get("http://tenant1.test/activities/1")
    assert stored is not None
    assert FIRM_NS.role.value not in stored, "a received firm: key is not kept"
    assert stored["@context"] == ["https://www.w3.org/ns/activitystreams", toot_context]
    assert stored["to"] == ["http://tenant1.test/user2"]
    assert stored["cc"] == []
    assert stored["toot:discoverable"] is True, "a vendor prefix survives"

    note = await tenant.public_store.get("http://tenant1.test/user2/note/1")
    assert note is not None
    assert FIRM_NS.privateKey.value not in note, "nor one inside the embedded object"
    assert note["_misskey_quote"] == "http://remote.test/notes/1"


async def test_a_received_foreign_activity_is_cached_without_firm_properties(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """#28: the remote cache holds the peer's copy, and that copy is stripped too."""
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
    context = make_context(tenant, principal=remote_identity, remote=_provider(StubTransport({})))

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        {
            "id": "http://remote.test/create11",
            "type": "Create",
            "actor": "http://remote.test/user1",
            "to": ["http://tenant1.test/user2"],
            FIRM_NS.collections.value: "http://remote.test/collections",
            "object": {
                "id": "http://remote.test/note/11",
                "type": "Note",
                FIRM_NS.blockedActor.value: "http://remote.test/user2",
            },
        },
    )

    assert (
        await tenant.public_store.get("http://remote.test/create11") is None
    ), "a foreign activity belongs in the remote cache"

    cached = await context.dereference("http://remote.test/create11")
    assert cached is not None
    assert FIRM_NS.collections.value not in cached, "the cached activity is stripped"
    assert cached["object"] == "http://remote.test/note/11"

    note = await context.dereference("http://remote.test/note/11")
    assert note is not None
    assert FIRM_NS.blockedActor.value not in note, "and so is the object it embedded"


async def test_an_inbox_handler_sees_the_activity_without_firm_properties(tenant: Tenant):
    """#28: the document ``process_inbox`` acts on is stripped, not only the stored copy."""
    owner = f"{tenant.prefix}/user1"
    followers = f"{owner}/followers"
    await tenant.public_store.put(
        {
            "id": owner,
            "type": "Person",
            "inbox": f"{owner}/inbox",
            "outbox": f"{owner}/outbox",
            "followers": followers,
        }
    )
    for box in (f"{owner}/inbox", f"{owner}/outbox", followers):
        await tenant.public_store.put(
            {"id": box, "type": "OrderedCollection", "attributedTo": owner, "items": []}
        )
    box_owner = cast(APActor, await tenant.public_store.get(owner))
    delivery = StubDeliveryService()

    await _arrive(
        tenant,
        cast(JSONObject, box_owner),
        {
            "id": REMOTE_REPLY_ID,
            "type": "Follow",
            "actor": REMOTE_AUTHOR,
            "object": owner,
            FIRM_NS.role.value: "admin",
        },
        delivery,
    )

    accept = next(activity for activity in delivery.activities if activity.get("type") == "Accept")
    seen = cast(JSONObject, accept["object"])
    assert seen["id"] == REMOTE_REPLY_ID, "the handler embeds the activity it acted on"
    assert FIRM_NS.role.value not in seen, "the handler never saw our namespace"
    assert seen["object"] == owner

    follow_box = await tenant.public_store.get(followers)
    assert follow_box is not None
    assert REMOTE_AUTHOR in cast(list[str], follow_box["items"]), "the follow ran"


OUTBOX_OWNER = "http://tenant1.test/user1"
OUTBOX_URI = f"{OUTBOX_OWNER}/outbox"
STANDARD_OBJECT_TYPES = [
    "Article",
    "Audio",
    "Document",
    "Event",
    "Image",
    "Note",
    "Page",
    "Place",
    "Profile",
    "Relationship",
    "Tombstone",
    "Video",
]


async def _seed_outbox(tenant: Tenant) -> None:
    await tenant.public_store.put(
        {
            "id": OUTBOX_OWNER,
            "type": "Person",
            "inbox": f"{OUTBOX_OWNER}/inbox",
            "outbox": OUTBOX_URI,
        }
    )
    await tenant.public_store.put(
        {"id": OUTBOX_URI, "type": "OrderedCollection", "attributedTo": OUTBOX_OWNER}
    )


async def test_outbox_post_wraps_a_bare_note_in_a_create(
    recording_service: tuple[ActivityPubService, StubDeliveryService], tenant: Tenant
):
    service, delivery = recording_service
    await _seed_outbox(tenant)
    note: JSONObject = {
        "type": "Note",
        "content": "hello",
        "to": ["http://remote.test/user1/followers", "as:Public"],
        "cc": "http://remote.test/user1",
        "bto": "http://remote.test/user2",
        "bcc": ["http://remote.test/user3"],
        "audience": ["http://remote.test/user4"],
    }

    location = await service.process_post(
        make_context(tenant, principal=StubIdentity(OUTBOX_OWNER, tenant)),
        StubUrl.parse(OUTBOX_URI),
        note,
    )

    assert location is not None
    create = await tenant.public_store.get(location)
    assert create is not None, "Location names the stored Create"
    assert create["type"] == "Create"
    assert create["actor"] == OUTBOX_OWNER
    stored_note = await tenant.public_store.get(cast(str, create["object"]))
    assert stored_note is not None, "the Create's object is the Note's own id"
    assert stored_note["type"] == "Note"
    assert stored_note["content"] == "hello"
    assert stored_note["attributedTo"] == OUTBOX_OWNER
    outbox = await tenant.public_store.get(OUTBOX_URI)
    assert outbox is not None
    assert outbox["items"] == [location], "the Create is the outbox entry, not the Note"
    assert delivery.delivered == [location], "the delivered activity is the Create"
    for key in ("to", "cc", "bto", "bcc", "audience"):
        assert sorted(cast(list[str], get_list(create, key))) == sorted(
            cast(list[str], get_list(note, key))
        ), f"wrong {key}"
        assert len(get_list(create, key)) == len(get_list(note, key)), f"duplicated {key}"


@pytest.mark.parametrize("object_type", STANDARD_OBJECT_TYPES)
async def test_outbox_post_wraps_every_standard_object_type(
    object_type: str, service: ActivityPubService, tenant: Tenant
):
    await _seed_outbox(tenant)
    document: JSONObject = {
        "type": object_type,
        "name": "test object",
        "to": "http://remote.test/user1",
        "cc": ["http://remote.test/user2", "as:Public"],
    }

    location = await service.process_post(
        make_context(tenant, principal=StubIdentity(OUTBOX_OWNER, tenant)),
        StubUrl.parse(OUTBOX_URI),
        document,
    )

    assert location is not None
    create = await tenant.public_store.get(location)
    assert create is not None
    assert create["type"] == "Create"
    stored = await tenant.public_store.get(cast(str, create["object"]))
    assert stored is not None
    assert stored["type"] == object_type
    assert sorted(cast(list[str], get_list(create, "to"))) == ["http://remote.test/user1"]
    assert sorted(cast(list[str], get_list(create, "cc"))) == sorted(
        cast(list[str], document["cc"])
    )


@pytest.mark.parametrize("document_type", ["Question", "Person", "OrderedCollection", "Link"])
async def test_outbox_post_does_not_wrap_a_non_object_type(
    document_type: str, service: ActivityPubService, tenant: Tenant
):
    await _seed_outbox(tenant)
    document: JSONObject = {
        "type": document_type,
        "name": "not content a Create introduces",
        "to": ["as:Public"],
    }

    location = await service.process_post(
        make_context(tenant, principal=StubIdentity(OUTBOX_OWNER, tenant)),
        StubUrl.parse(OUTBOX_URI),
        document,
    )

    assert location is not None
    stored = await tenant.public_store.get(location)
    assert stored is not None
    assert stored["type"] == document_type, "stored and delivered as itself"
    outbox = await tenant.public_store.get(OUTBOX_URI)
    assert outbox is not None
    assert outbox.get("items", []) == [location], "the document itself is the outbox entry"


async def test_outbox_post_does_not_wrap_a_multityped_activity(
    service: ActivityPubService, tenant: Tenant
):
    await _seed_outbox(tenant)
    document: JSONObject = {"type": ["Note", "Question"], "name": "activity wins"}

    location = await service.process_post(
        make_context(tenant, principal=StubIdentity(OUTBOX_OWNER, tenant)),
        StubUrl.parse(OUTBOX_URI),
        document,
    )

    assert location is not None
    stored = await tenant.public_store.get(location)
    assert stored is not None
    assert stored["type"] == ["Note", "Question"], "naming an activity type beats the object list"


async def test_outbox_post_wraps_an_unknown_object_type(
    service: ActivityPubService, tenant: Tenant
):
    await _seed_outbox(tenant)
    document: JSONObject = {
        "type": "Poll",
        "name": "caller's own vocabulary",
        "to": ["as:Public"],
    }

    location = await service.process_post(
        make_context(tenant, principal=StubIdentity(OUTBOX_OWNER, tenant)),
        StubUrl.parse(OUTBOX_URI),
        document,
    )

    assert location is not None
    create = await tenant.public_store.get(location)
    assert create is not None
    assert create["type"] == "Create", "a type AS2 does not define is an object type"
    stored = await tenant.public_store.get(cast(str, create["object"]))
    assert stored is not None
    assert stored["type"] == "Poll"
    outbox = await tenant.public_store.get(OUTBOX_URI)
    assert outbox is not None
    assert outbox["items"] == [location], "the Create is the outbox entry"
