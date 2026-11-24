import copy
import logging
from typing import Any, Sequence, cast

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
    mastodon_deliver_shared_inbox_activity,
    post_to_inbox,
)
from firm.core.services.activitypub.support import FirmBoxContext
from firm.core.store.file import FileResourceStore
from firm.core.store.memory import MemoryResourceStore
from firm.core.util import AP_PUBLIC_URIS, get_list, is_public
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

AP_PUBLIC = AP_PUBLIC_URIS[0]


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


async def _outbox_media_fixture(tenant: Tenant, media_uri: str, media_url: str) -> None:
    """An actor with an outbox, and one uploaded-but-unaddressed media object."""
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
            {
                "id": media_uri,
                "type": "Image",
                "url": media_url,
                "mediaType": "image/png",
                "attributedTo": "http://tenant1.test/user2",
            },
        ],
    )


async def test_a_public_note_publishes_the_media_it_attaches(tenant: Tenant) -> None:
    """The note decides the file's audience: an upload has none of its own.

    A file is stored before the note that references it exists, so it can only answer
    to its uploader until a note says who else may read it. Serving the bytes under the
    same policy as the object means a public note's attachment has to be Public, or no
    peer can fetch the picture it is showing.
    """
    service = ActivityPubService(StubAuthorizationService(), StubDeliveryService())
    media_uri = "http://tenant1.test/objects/image/1"
    media_url = "http://tenant1.test/actors/user2/media/abc.png"
    await _outbox_media_fixture(tenant, media_uri, media_url)

    await service.process_post(
        make_context(tenant, principal=StubIdentity("http://tenant1.test/user2", tenant)),
        StubUrl.parse("http://tenant1.test/outbox"),
        {
            "id": "http://tenant1.test/create1",
            "type": "Create",
            "actor": "http://tenant1.test/user2",
            "to": [AP_PUBLIC],
            "object": {
                "type": "Note",
                "content": "a picture",
                "attachment": [media_uri],
                "to": [AP_PUBLIC],
            },
        },
    )

    published = await tenant.public_store.get(media_uri)
    assert published is not None
    assert is_public(published), "a public note's attachment is readable by its readers"


async def test_a_direct_note_publishes_its_media_to_that_recipient_only(tenant: Tenant) -> None:
    """Widening is the note's audience, not Public: a direct note attaches a private file."""
    service = ActivityPubService(StubAuthorizationService(), StubDeliveryService())
    media_uri = "http://tenant1.test/objects/image/2"
    media_url = "http://tenant1.test/actors/user2/media/def.png"
    await _outbox_media_fixture(tenant, media_uri, media_url)

    await service.process_post(
        make_context(tenant, principal=StubIdentity("http://tenant1.test/user2", tenant)),
        StubUrl.parse("http://tenant1.test/outbox"),
        {
            "id": "http://tenant1.test/create2",
            "type": "Create",
            "actor": "http://tenant1.test/user2",
            "to": ["http://tenant1.test/user3"],
            "object": {
                "type": "Note",
                "content": "just for you",
                "attachment": [media_uri],
                "to": ["http://tenant1.test/user3"],
            },
        },
    )

    published = await tenant.public_store.get(media_uri)
    assert published is not None
    assert not is_public(published), "a direct note does not publish its attachment"
    assert "http://tenant1.test/user3" in get_list(published, "to"), "its recipient may read it"


async def test_an_outbox_delete_keeps_a_tombstone_not_the_object_it_names(tenant: Tenant):
    """The handler owns the payload: what a Delete carries does not come back as a copy."""
    service = ActivityPubService(StubAuthorizationService(), StubDeliveryService())
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
            {
                "id": "http://tenant1.test/notes/1",
                "type": "Note",
                "attributedTo": "http://tenant1.test/user2",
                "content": "hello",
            },
        ],
    )

    await service.process_post(
        make_context(tenant, principal=StubIdentity("http://tenant1.test/user2", tenant)),
        StubUrl.parse("http://tenant1.test/outbox"),
        {
            "id": "http://tenant1.test/delete1",
            "type": "Delete",
            "actor": "http://tenant1.test/user2",
            "object": {
                "id": "http://tenant1.test/notes/1",
                "type": "Note",
                "content": "hello",
            },
        },
    )

    note = await tenant.public_store.get("http://tenant1.test/notes/1")
    assert note is not None and note["type"] == "Tombstone", "the payload is not stored back"
    outbox = await tenant.public_store.get("http://tenant1.test/outbox")
    assert outbox is not None
    activity_id = cast(list[str], outbox["items"])[-1]
    assert activity_id.startswith("http://tenant1.test/user2/delete/")
    stored = await tenant.public_store.get(activity_id)
    assert stored is not None
    assert stored["object"] == "http://tenant1.test/notes/1", "the activity keeps a reference"


async def test_keep_does_not_write_an_embedded_node_over_an_existing_document(tenant: Tenant):
    """The reported bug: a fragment about a channel must not replace the channel.

    PeerTube's published Video example writes ``attributedTo`` as an embedded
    ``{type: Group, id}`` node. Storing that node over the channel document leaves an
    actor with no outbox, no followers and no owning account.
    """
    context = make_context(tenant)
    channel_uri = f"{tenant.prefix}/user1_channel"
    await tenant.public_store.put(
        {
            "id": channel_uri,
            "type": "Group",
            "preferredUsername": "user1_channel",
            "attributedTo": [{"type": "Person", "id": f"{tenant.prefix}/user1"}],
            "outbox": f"{channel_uri}/outbox",
            "followers": f"{channel_uri}/followers",
        }
    )

    await context.keep(
        {
            "id": f"{tenant.prefix}/user1/video/1",
            "type": "Video",
            "name": "A walk",
            "attributedTo": [{"type": "Group", "id": channel_uri}],
        }
    )

    channel = await tenant.public_store.get(channel_uri)
    assert channel is not None
    assert channel["outbox"] == f"{channel_uri}/outbox", "the channel kept its boxes"
    assert channel["attributedTo"] == [{"type": "Person", "id": f"{tenant.prefix}/user1"}]
    video = await tenant.public_store.get(f"{tenant.prefix}/user1/video/1")
    assert video is not None
    assert video["attributedTo"] == [channel_uri], "the video keeps a reference"


async def test_keep_does_not_overwrite_a_cached_remote_document(tenant: Tenant):
    """A cached copy is what this server serves; a fragment about it replaces nothing."""
    transport = StubTransport({})
    provider = _provider(transport)
    context = make_context(tenant, remote=provider)
    actor_uri = "https://remote.test/users/a"
    await provider.remember({"id": actor_uri, "type": "Person", "inbox": f"{actor_uri}/inbox"})

    await context.keep(
        {
            "id": "https://remote.test/activities/9",
            "type": "Create",
            "object": {"id": actor_uri, "type": "Person", "name": "a stub"},
        }
    )

    cached = await provider.peek(actor_uri)
    assert cached is not None
    assert cached["inbox"] == f"{actor_uri}/inbox", "the cached actor survives"
    assert "name" not in cached
    assert transport.urls == [], "nothing was refetched"


async def test_an_embedded_node_does_not_create_a_document_the_request_does_not_own(
    tenant: Tenant,
):
    """A request may bring its own namespace into existence, not another's."""
    context = make_context(tenant, principal=StubIdentity(f"{tenant.prefix}/user2", tenant))

    await context.keep(
        {
            "id": f"{tenant.prefix}/user2/activities/1",
            "type": "Create",
            "object": {"id": f"{tenant.prefix}/user3/note/1", "type": "Note"},
        }
    )

    assert await tenant.public_store.get(f"{tenant.prefix}/user3/note/1") is None
    stored = await tenant.public_store.get(f"{tenant.prefix}/user2/activities/1")
    assert stored is not None
    assert stored["object"] == f"{tenant.prefix}/user3/note/1", "kept as a reference"


async def test_keep_stores_as_the_actor_it_is_given(tenant: Tenant):
    """A reply is stored as its sender, which is not the request's principal.

    An ``Accept`` to a remote ``Follow`` is published while the principal is still the
    follower, so who the document is being stored as has to be said explicitly.
    """
    context = make_context(tenant, principal=StubIdentity("https://remote.test/follower", tenant))
    actor_uri = f"{tenant.prefix}/user1"
    accept_uri = f"{actor_uri}/accept/1"

    kept = await context.keep(
        {"id": accept_uri, "type": "Accept", "actor": actor_uri},
        actor_uri=actor_uri,
    )

    assert kept == accept_uri
    assert await tenant.public_store.get(accept_uri) is not None


async def test_an_outbox_update_replaces_a_document_its_actor_owns(tenant: Tenant):
    service = ActivityPubService(StubAuthorizationService(), StubDeliveryService())
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
            {
                "id": "http://tenant1.test/notes/1",
                "type": "Note",
                "attributedTo": "http://tenant1.test/user2",
                "content": "before",
            },
        ],
    )

    await service.process_post(
        make_context(tenant, principal=StubIdentity("http://tenant1.test/user2", tenant)),
        StubUrl.parse("http://tenant1.test/outbox"),
        {
            "id": "http://tenant1.test/update1",
            "type": "Update",
            "actor": "http://tenant1.test/user2",
            "object": {
                "id": "http://tenant1.test/notes/1",
                "type": "Note",
                "attributedTo": "http://tenant1.test/user2",
                "content": "after",
            },
        },
    )

    note = await tenant.public_store.get("http://tenant1.test/notes/1")
    assert note is not None and note["content"] == "after", "Update is the replacement path"


async def test_an_outbox_update_of_another_actors_document_is_refused(tenant: Tenant):
    service = ActivityPubService(StubAuthorizationService(), StubDeliveryService())
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
            {
                "id": "http://tenant1.test/notes/3",
                "type": "Note",
                "attributedTo": "http://tenant1.test/user3",
                "content": "theirs",
            },
        ],
    )

    with pytest.raises(NotAuthorizedException):
        await service.process_post(
            make_context(tenant, principal=StubIdentity("http://tenant1.test/user2", tenant)),
            StubUrl.parse("http://tenant1.test/outbox"),
            {
                "id": "http://tenant1.test/update2",
                "type": "Update",
                "actor": "http://tenant1.test/user2",
                "object": {
                    "id": "http://tenant1.test/notes/3",
                    "type": "Note",
                    "attributedTo": "http://tenant1.test/user2",
                    "content": "stolen",
                },
            },
        )

    note = await tenant.public_store.get("http://tenant1.test/notes/3")
    assert note is not None and note["content"] == "theirs"


async def test_an_outbox_add_does_not_rewrite_the_object_it_names(tenant: Tenant):
    """Add puts an existing object in a collection; it does not describe it."""
    service = ActivityPubService(StubAuthorizationService(), StubDeliveryService())
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
            {
                "id": "http://tenant1.test/collection/1",
                "type": "OrderedCollection",
                "attributedTo": "http://tenant1.test/user2",
                "orderedItems": [],
            },
            {
                "id": "http://tenant1.test/notes/4",
                "type": "Note",
                "attributedTo": "http://tenant1.test/user2",
                "content": "original",
            },
        ],
    )

    await service.process_post(
        make_context(tenant, principal=StubIdentity("http://tenant1.test/user2", tenant)),
        StubUrl.parse("http://tenant1.test/outbox"),
        {
            "id": "http://tenant1.test/add1",
            "type": "Add",
            "actor": "http://tenant1.test/user2",
            "target": "http://tenant1.test/collection/1",
            "object": {
                "id": "http://tenant1.test/notes/4",
                "type": "Note",
                "content": "replaced by an Add",
            },
        },
    )

    note = await tenant.public_store.get("http://tenant1.test/notes/4")
    assert note is not None and note["content"] == "original"
    collection = await tenant.public_store.get("http://tenant1.test/collection/1")
    assert collection is not None
    assert cast(list[str], collection["items"]) == ["http://tenant1.test/notes/4"]


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


def embedded_object(document: JSONObject) -> JSONObject:
    """The object of a served Create, narrowed for type checkers."""
    return cast(JSONObject, document["object"])


async def _store_photo_note(tenant: Tenant) -> tuple[JSONObject, JSONObject]:
    """A note as firm stores it: the media document is a reference, not an embedded object."""
    document: JSONObject = {
        "id": "http://tenant1.test/actors/tester/document/1",
        "type": "Document",
        "mediaType": "image/jpeg",
        "name": "Snakes!",
        "url": "http://tenant1.test/tester/media/snakes.jpg",
        "attributedTo": "http://tenant1.test/actors/tester",
    }
    note: JSONObject = {
        "id": "http://tenant1.test/actors/tester/note/1",
        "type": "Note",
        "attributedTo": "http://tenant1.test/actors/tester",
        "content": "Snakes!",
        "attachment": [document["id"]],
    }
    await tenant.public_store.put(document)
    await tenant.public_store.put(note)
    return document, note


async def test_serialize_embeds_an_attachment_reference_on_the_object(tenant: Tenant):
    document, note = await _store_photo_note(tenant)
    activity: JSONObject = {
        "id": "http://tenant1.test/activities/create/1",
        "type": "Create",
        "actor": "http://tenant1.test/actors/tester",
        "object": note["id"],
    }

    served = await serialize(make_context(tenant), activity)

    # Pixelfed reads attachment[i].url; handed a bare IRI it shows a post with no media.
    assert embedded_object(served)["attachment"] == [document]


async def test_serialize_embeds_an_attachment_reference_in_a_collection_item(tenant: Tenant):
    document, note = await _store_photo_note(tenant)
    await tenant.public_store.put(
        {
            "id": "http://tenant1.test/activities/create/1",
            "type": "Create",
            "actor": "http://tenant1.test/actors/tester",
            "object": note["id"],
        }
    )
    collection: JSONObject = {
        "id": "http://tenant1.test/actors/tester/outbox",
        "type": "OrderedCollection",
        "items": ["http://tenant1.test/activities/create/1"],
        "totalItems": 1,
    }

    served = await serialize(make_context(tenant), collection)

    items = cast(list[Any], served["orderedItems"])
    assert embedded_object(cast(JSONObject, items[0]))["attachment"] == [document]


async def test_serialize_does_not_mutate_the_stored_attachment(tenant: Tenant):
    _, note = await _store_photo_note(tenant)
    activity: JSONObject = {
        "id": "http://tenant1.test/activities/create/1",
        "type": "Create",
        "actor": "http://tenant1.test/actors/tester",
        "object": note["id"],
    }

    await serialize(make_context(tenant), activity)

    stored = await tenant.public_store.get("http://tenant1.test/actors/tester/note/1")
    assert stored is not None
    assert stored["attachment"] == ["http://tenant1.test/actors/tester/document/1"]


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


async def test_a_collection_item_is_served_with_its_creates_object_embedded(tenant: Tenant):
    """A stored activity holds a reference; serving reverses that, item by item.

    A reader must not be handed a link to something it cannot fetch - a peer's
    non-public object is 404 to everyone, including us.
    """
    note_uri = "http://tenant1.test/user2/note/7"
    create_uri = "http://tenant1.test/user2/create/7"
    note: JSONObject = {"id": note_uri, "type": "Note", "content": "Wazzup?"}
    await tenant.public_store.put(note)
    await tenant.public_store.put({"id": create_uri, "type": "Create", "object": note_uri})

    page: JSONObject = {
        "id": "http://tenant1.test/user2/inbox",
        "type": "OrderedCollectionPage",
        "items": [create_uri],
    }

    served = await serialize(make_context(tenant), page)

    items = cast(list[JSONObject], served["orderedItems"])
    assert items[0]["object"] == note, "the item carries the document, not a reference"

    stored = await tenant.public_store.get(create_uri)
    assert stored is not None and stored["object"] == note_uri, "the store keeps the reference"


async def test_a_collection_item_keeps_its_reference_when_the_object_is_unavailable(
    tenant: Tenant,
):
    """An object that cannot be resolved does not remove the item from the page."""
    object_uri = "http://remote.test/note/gone"
    create_uri = "http://tenant1.test/user2/create/8"
    await tenant.public_store.put({"id": create_uri, "type": "Create", "object": object_uri})

    page: JSONObject = {
        "id": "http://tenant1.test/user2/inbox",
        "type": "OrderedCollectionPage",
        "items": [create_uri],
    }

    served = await serialize(make_context(tenant), page)

    items = cast(list[JSONObject], served["orderedItems"])
    item = items[0]
    assert item["id"] == create_uri, "the item is still served"
    assert item["object"] == object_uri, "an unresolvable object stays a reference"


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


async def test_shared_inbox_read_is_not_served(service: ActivityPubService, tenant: Tenant):
    """A GET of the advertised shared inbox is not a resource read.

    The endpoint takes delivery only, so nothing is stored at its URI: a peer that
    follows up its POST with a read gets a 404 rather than a paged collection.
    """
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/shared"

    await tenant.public_store.put(
        {
            "id": "http://tenant1.test/obj1",
            "type": "Create",
            "to": ["as:Public"],
            "actor": "http://remote.test/user1",
            "object": "http://tenant1.test/obj1",
        }
    )
    with pytest.raises(NotFoundException):
        await service.process_get(
            make_context(tenant),
            StubUrl.parse("http://tenant1.test/shared?offset=0"),
        )


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


async def test_inbox_announce_creates_the_shares_collection_of_our_object(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """Announcing an object we own adds the announce to that object's shares.

    The collection has to exist and the object has to name it: an id with no document
    behind it makes the store raise, and the sender retries the resulting 5xx forever.
    """
    tenant = remote_identity.tenant
    note_uri = "http://tenant1.test/user2/note"
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
                "id": note_uri,
                "type": "Note",
                "attributedTo": "http://tenant1.test/user2",
            },
        ],
    )
    transport = StubTransport({})
    provider = _provider(transport)
    activity: JSONObject = {
        "id": "http://remote.test/announce1",
        "type": "Announce",
        "actor": "http://remote.test/user1",
        "object": note_uri,
    }

    accepted = await service.process_post(
        make_context(tenant, principal=remote_identity, remote=provider),
        StubUrl.parse("http://tenant1.test/inbox"),
        activity,
    )
    assert accepted is None, "the route answers 200, not a 5xx the sender retries"

    shares = await tenant.public_store.get(f"{note_uri}/shares")
    assert shares is not None, "the collection exists, so an item can go into it"
    assert shares["items"] == ["http://remote.test/announce1"]
    note = await tenant.public_store.get(note_uri)
    assert note is not None
    assert note["shares"] == f"{note_uri}/shares", "the object names the collection"
    assert transport.urls == [], "an object we own is never fetched from the network"

    await service.process_post(
        make_context(tenant, principal=remote_identity, remote=provider),
        StubUrl.parse("http://tenant1.test/inbox"),
        dict(activity),
    )
    shares = await tenant.public_store.get(f"{note_uri}/shares")
    assert shares is not None
    assert shares["items"] == [
        "http://remote.test/announce1"
    ], "a redelivered announce is listed once"


async def test_inbox_announce_of_a_remote_object_writes_no_shares(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """The object's origin maintains its shares; we only record the announce."""
    tenant = remote_identity.tenant
    remote_note = "http://remote.test/note/1"
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
    transport = StubTransport(
        {
            remote_note: json_response(
                {
                    "id": remote_note,
                    "type": "Note",
                    "attributedTo": "http://remote.test/user1",
                }
            )
        }
    )
    provider = _provider(transport)
    activity: JSONObject = {
        "id": "http://remote.test/announce2",
        "type": "Announce",
        "actor": "http://remote.test/user1",
        "object": remote_note,
    }

    accepted = await service.process_post(
        make_context(tenant, principal=remote_identity, remote=provider),
        StubUrl.parse("http://tenant1.test/inbox"),
        activity,
    )
    assert accepted is None

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox is not None and inbox["items"] == ["http://remote.test/announce2"]
    assert (
        await tenant.public_store.get(f"{remote_note}/shares") is None
    ), "a document owned by another server is not written here"
    assert transport.urls == [], "and it is not read either: its shares are its origin's"


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
            "id": "http://remote.test/objects/1",
            "type": "Document",
            "content": "Some stuff...",
        },
    }
    context = make_context(tenant, principal=remote_identity, remote=_provider(StubTransport({})))
    await service.process_post(context, StubUrl.parse("http://tenant1.test/inbox"), resource)

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox and isinstance(inbox["items"], Sequence)
    assert inbox["items"] == ["http://remote.test/create1"], "the activity is still listed"

    assert (
        await tenant.public_store.get("http://remote.test/create1") is None
    ), "the received activity belongs to another server"
    assert (
        await tenant.public_store.query({"type": "Document"})
    ) == [], "a sender's object is not a tenant document"
    document = await context.resolver.peek("http://remote.test/objects/1")
    assert document is not None and document["content"] == "Some stuff...", "kept as theirs"


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
    ) is None, "another server may not create a document under our prefix"

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox is not None and inbox["items"] == ["http://remote.test/create9"], "still listed"

    remembered = await context.resolver.fetch("http://remote.test/create9")
    assert remembered is not None and remembered["id"] == "http://remote.test/create9"
    assert transport.urls == [], "remembered in the remote cache, not refetched"


async def test_inbox_create_fetches_a_referenced_object(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """A Create may carry only a reference, and the object must be read at intake.

    A read deferred until the inbox page is served may never succeed: snac2 answers
    404 for a non-public object whoever asks, so a direct message fetched later is
    lost. The object has to be read while the sender is still offering it.
    """
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
    note: JSONObject = {
        "id": "http://remote.test/note/7",
        "type": "Note",
        "attributedTo": "http://remote.test/user1",
        "to": ["http://tenant1.test/user2"],
        "content": "a private reply",
    }
    transport = StubTransport({"http://remote.test/note/7": json_response(note)})
    context = make_context(tenant, principal=remote_identity, remote=_provider(transport))

    resource: JSONObject = {
        "id": "http://remote.test/create7",
        "type": "Create",
        "actor": "http://remote.test/user1",
        "object": "http://remote.test/note/7",
    }
    await service.process_post(context, StubUrl.parse("http://tenant1.test/inbox"), resource)

    assert transport.urls == ["http://remote.test/note/7"], "the referenced object is read once"

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox is not None and inbox["items"] == ["http://remote.test/create7"]

    remembered = await context.resolver.fetch("http://remote.test/note/7")
    assert remembered is not None and remembered["content"] == "a private reply"
    assert transport.urls == ["http://remote.test/note/7"], "kept, so serving it needs no refetch"


async def test_inbox_create_survives_an_unfetchable_referenced_object(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """A 404 for the referenced object must not fail the delivery.

    The sender retries a 5xx forever, and the activity is the record that the
    object was announced to us: keep it and log, rather than raise.
    """
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
    transport = StubTransport({})  # an unscripted GET answers 404
    context = make_context(tenant, principal=remote_identity, remote=_provider(transport))

    resource: JSONObject = {
        "id": "http://remote.test/create8",
        "type": "Create",
        "actor": "http://remote.test/user1",
        "object": "http://remote.test/note/8",
    }
    await service.process_post(context, StubUrl.parse("http://tenant1.test/inbox"), resource)

    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox is not None and inbox["items"] == ["http://remote.test/create8"]

    remembered = await context.resolver.fetch("http://remote.test/create8")
    assert remembered is not None, "the activity is on record even without its object"


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


async def test_a_store_backed_relationship_collection_is_served_with_its_items(tmp_path):
    """The list lives in the item store, so the served document has to carry it."""
    prefix = "http://tenant1.test"
    store = FileResourceStore(tmp_path, "public")
    tenant = Tenant(prefix, store, MemoryResourceStore(), tmp_path)
    collection_uri = f"{prefix}/user1/following"
    await store.put(
        {
            "id": collection_uri,
            "type": "Collection",
            "attributedTo": f"{prefix}/user1",
            "totalItems": 0,
        }
    )
    first, second = f"{prefix}/user2", f"{prefix}/user3"
    for uri in (first, second):
        await store.put({"id": uri, "type": "Person"})
        await store.insert_item(collection_uri, uri)
    transport = StubTransport()
    context = make_context(tenant, remote=_provider(transport))

    stored = await store.get(collection_uri)
    assert stored is not None

    served = await serialize(context, stored)

    assert served["items"] == [second, first], "the store's items are served as IRIs"
    assert served["totalItems"] == 2
    assert transport.urls == [], "a relationship collection is not dereferenced"


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
    actor = f"{tenant.prefix}/user1"
    activity: JSONObject = {
        "id": f"{tenant.prefix}/activities/1",
        "type": "Create",
        "actor": actor,
    }
    delivery = StubDeliveryService()
    delivery.failures = [DeliveryFailure("https://remote.test/inbox", 3, "HTTP 503")]
    base = make_context(tenant)
    context = FirmBoxContext(
        base.resolver,
        tenant,
        base.principal,
        base.authorizer,
        box_owner=cast(APActor, {"id": actor, "type": "Person", "outbox": outbox}),
        box_uri=outbox,
        delivery_service=delivery,
    )

    with caplog.at_level(logging.ERROR, logger="firm.core.services.activitypub.publish"):
        await outbox_send(context, activity)

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


async def _arrive(
    tenant, box_owner: JSONObject, activity: JSONObject, delivery, principal=None
) -> None:
    context = make_context(
        tenant,
        principal=principal or StubIdentity(REMOTE_AUTHOR, tenant),
    )
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
    actor_uri = f"{tenant.prefix}/user1"
    activity = _reply(followers, note, actor=actor_uri)

    await _arrive(
        tenant,
        box_owner,
        activity,
        delivery,
        principal=StubIdentity(actor_uri, tenant),
    )

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


async def test_a_received_activity_keeps_the_senders_firm_properties(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """``firm:`` is our vocabulary: a sender's copy is the sender's claim, and it is kept.

    Peers may learn the terms, so stripping them on ingress would cost them the terms
    they sent us and would mean a client could never author one.
    """
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
    context = make_context(
        tenant,
        principal=StubIdentity("http://tenant1.test/user2", tenant),
        remote=_provider(StubTransport({})),
    )
    toot_context: JSONObject = {"toot": "http://joinmastodon.org/ns#"}

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        {
            "id": "http://tenant1.test/user2/activities/1",
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

    stored = await tenant.public_store.get("http://tenant1.test/user2/activities/1")
    assert stored is not None
    assert stored[FIRM_NS.role.value] == "admin", "the sender's firm: claim is kept"
    assert stored["@context"] == ["https://www.w3.org/ns/activitystreams", toot_context]
    assert stored["to"] == ["http://tenant1.test/user2"]
    assert stored["cc"] == []
    assert stored["toot:discoverable"] is True, "a vendor prefix survives"

    note = await tenant.public_store.get("http://tenant1.test/user2/note/1")
    assert note is not None
    assert note[FIRM_NS.privateKey.value] == "leak", "nor is one inside the embedded object"
    assert note["_misskey_quote"] == "http://remote.test/notes/1"


async def test_a_received_activity_drops_urn_properties(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """A ``urn:`` names a document of ours, which no peer can serve: the key goes."""
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
    context = make_context(
        tenant,
        principal=StubIdentity("http://tenant1.test/user2", tenant),
        remote=_provider(StubTransport({})),
    )

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        {
            "id": "http://tenant1.test/user2/activities/2",
            "type": "Create",
            "actor": "http://tenant1.test/user2",
            "to": ["http://tenant1.test/user2"],
            "urn:firm:role": "admin",
            FIRM_NS.role.value: "admin",
            "object": {
                "id": "http://tenant1.test/user2/note/2",
                "type": "Note",
                "urn:firm:privateKey": "leak",
                FIRM_NS.privateKey.value: "claim",
            },
        },
    )

    stored = await tenant.public_store.get("http://tenant1.test/user2/activities/2")
    assert stored is not None
    assert _urn_keys(stored) == [], "no urn: property survives anywhere in the document"
    assert stored[FIRM_NS.role.value] == "admin", "a firm: key is a different matter"

    note = await tenant.public_store.get("http://tenant1.test/user2/note/2")
    assert note is not None
    assert _urn_keys(note) == []
    assert note[FIRM_NS.privateKey.value] == "claim"


def _urn_keys(document: JSON) -> list[str]:
    """Every mapping key in the document that is a URN."""
    if isinstance(document, dict):
        found = [str(key) for key in document if str(key).startswith("urn:")]
        for value in document.values():
            found.extend(_urn_keys(value))
        return found
    if isinstance(document, list):
        return [key for item in document for key in _urn_keys(item)]
    return []


async def test_an_activity_addressed_by_a_urn_is_dropped(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """Nothing stores or records a URN-addressed activity: a peer cannot serve one."""
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
    context = make_context(
        tenant,
        principal=StubIdentity("http://tenant1.test/user2", tenant),
        remote=_provider(StubTransport({})),
    )

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        {
            "id": "urn:uuid:0d6c4f4a-5d19-4e0e-9f31-2b9a1f0f0b7c",
            "type": "Create",
            "actor": "http://tenant1.test/user2",
            "to": ["http://tenant1.test/user2"],
            "object": {"id": "http://tenant1.test/user2/note/3", "type": "Note"},
        },
    )

    assert (
        await tenant.public_store.get("urn:uuid:0d6c4f4a-5d19-4e0e-9f31-2b9a1f0f0b7c") is None
    ), "a URN-addressed document is never stored"
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox is not None
    assert get_list(inbox, "items") == [], "and never recorded in the box"
    assert (
        await tenant.public_store.get("http://tenant1.test/user2/note/3") is None
    ), "the object arrived with an activity we dropped"


async def test_an_embedded_object_addressed_by_a_urn_drops_the_activity(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """An object no peer can serve cannot be kept, and the activity naming it is empty."""
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
    context = make_context(
        tenant,
        principal=StubIdentity("http://tenant1.test/user2", tenant),
        remote=_provider(StubTransport({})),
    )

    await service.process_post(
        context,
        StubUrl.parse("http://tenant1.test/inbox"),
        {
            "id": "http://tenant1.test/activities/3",
            "type": "Create",
            "actor": "http://tenant1.test/user2",
            "to": ["http://tenant1.test/user2"],
            "object": {"id": "urn:uuid:6b1c2a0e-3a4f-4a1c-8a55-9f0f5d3b7c11", "type": "Note"},
        },
    )

    assert await tenant.public_store.get("http://tenant1.test/activities/3") is None
    assert await tenant.public_store.get("urn:uuid:6b1c2a0e-3a4f-4a1c-8a55-9f0f5d3b7c11") is None
    inbox = await tenant.public_store.get("http://tenant1.test/inbox")
    assert inbox is not None
    assert get_list(inbox, "items") == []


async def test_keep_drops_a_document_addressed_by_a_urn(tenant: Tenant):
    """The choke point for every received document, whichever handler asked for it."""
    context = make_context(tenant)
    urn_uri = "urn:uuid:9d2a1b6e-0f3c-4c47-9c5a-1f4b8e2d7a60"

    await context.keep({"id": urn_uri, "type": "Note"})

    assert await tenant.public_store.is_stored(urn_uri) is False


async def test_keep_stores_an_embedded_document_apart_and_references_it(tenant: Tenant):
    """A kept document is a CBD: what it embeds is a document of its own."""
    context = make_context(tenant)
    activity_uri = f"{tenant.prefix}/activities/1"
    note_uri = f"{tenant.prefix}/notes/1"
    activity: JSONObject = {
        "id": activity_uri,
        "type": "Create",
        "actor": f"{tenant.prefix}/user1",
        "object": {"id": note_uri, "type": "Note", "content": "hello"},
    }

    assert await context.keep(activity) == activity_uri

    stored = await tenant.public_store.get(activity_uri)
    assert stored is not None
    assert stored["object"] == note_uri, "the activity keeps a reference, not a copy"
    note = await tenant.public_store.get(note_uri)
    assert note is not None and note["content"] == "hello"
    assert isinstance(activity["object"], dict), "the caller's document is not rewritten"


async def test_keep_splits_an_embedded_document_at_any_depth(tenant: Tenant):
    """A node without an address stays inline, and what it embeds still splits out."""
    context = make_context(tenant)
    note_uri = f"{tenant.prefix}/notes/1"
    reply_uri = f"{tenant.prefix}/notes/2"

    await context.keep(
        {
            "id": note_uri,
            "type": "Note",
            "replies": {
                "type": "Collection",
                "items": [{"id": reply_uri, "type": "Note", "content": "a reply"}],
            },
        }
    )

    stored = await tenant.public_store.get(note_uri)
    assert stored is not None
    replies = cast(JSONObject, stored["replies"])
    assert replies["items"] == [reply_uri], "the blank node stays, its child does not"
    reply = await tenant.public_store.get(reply_uri)
    assert reply is not None and reply["content"] == "a reply"


async def test_keep_leaves_a_fragment_addressed_node_inline(tenant: Tenant):
    """A fragment names a part of the document that carries it: nothing fetches it apart."""
    context = make_context(tenant)
    actor_uri = f"{tenant.prefix}/actors/1"

    await context.keep(
        {
            "id": actor_uri,
            "type": "Person",
            "publicKey": {
                "id": f"{actor_uri}#key",
                "owner": actor_uri,
                "publicKeyPem": "PEM",
            },
        }
    )

    stored = await tenant.public_store.get(actor_uri)
    assert stored is not None
    key = cast(JSONObject, stored["publicKey"])
    assert key["publicKeyPem"] == "PEM"
    assert await tenant.public_store.is_stored(f"{actor_uri}#key") is False


async def test_keep_stores_a_collections_object_items_as_references(tenant: Tenant):
    """What a collection holds is a membership of URIs, whatever arrived in it."""
    context = make_context(tenant)
    collection_uri = f"{tenant.prefix}/collections/1"
    note_uri = f"{tenant.prefix}/notes/3"
    bare_uri = f"{tenant.prefix}/notes/4"

    await context.keep(
        {
            "id": collection_uri,
            "type": "Collection",
            "items": [{"id": note_uri, "type": "Note"}, bare_uri],
        }
    )

    stored = await tenant.public_store.get(collection_uri)
    assert stored is not None
    assert stored["items"] == [note_uri, bare_uri]
    assert await tenant.public_store.is_stored(note_uri)


async def test_keep_breaks_a_reference_cycle(tenant: Tenant):
    """Two documents that name each other are stored once each, not forever."""
    context = make_context(tenant)
    a_uri = f"{tenant.prefix}/notes/a"
    b_uri = f"{tenant.prefix}/notes/b"

    await context.keep(
        {
            "id": a_uri,
            "type": "Note",
            "inReplyTo": {
                "id": b_uri,
                "type": "Collection",
                "items": [{"id": a_uri, "type": "Note"}],
            },
        }
    )

    stored_b = await tenant.public_store.get(b_uri)
    assert stored_b is not None
    assert stored_b["items"] == [a_uri], "the ancestor is a reference, not another copy"


async def test_keep_caches_a_remote_document_it_embeds(tenant: Tenant):
    """Who owns the bytes is where they are kept, at every depth."""
    provider = _provider(StubTransport({}))
    context = make_context(tenant, remote=provider)
    local_uri = f"{tenant.prefix}/notes/9"
    remote_uri = "http://remote.test/note/9"

    await context.keep(
        {
            "id": local_uri,
            "type": "Note",
            "inReplyTo": {"id": remote_uri, "type": "Note", "content": "theirs"},
        }
    )

    stored = await tenant.public_store.get(local_uri)
    assert stored is not None
    assert stored["inReplyTo"] == remote_uri
    assert await tenant.public_store.is_stored(remote_uri) is False
    assert await provider.peek(remote_uri) is not None


async def test_keep_references_another_tenants_document_without_copying_it(tmp_path):
    """A document another tenant of this instance owns is left where its owner keeps it."""
    a, b = _two_tenants(tmp_path)
    other_uri = "http://b.test/note/1"
    await b.public_store.put({"id": other_uri, "type": "Note", "content": "theirs"})
    context = make_context(a, tenants={"http://a.test": a, "http://b.test": b})

    await context.keep(
        {
            "id": "http://a.test/notes/1",
            "type": "Note",
            "inReplyTo": {"id": other_uri, "type": "Note", "content": "theirs"},
        }
    )

    stored = await a.public_store.get("http://a.test/notes/1")
    assert stored is not None
    assert stored["inReplyTo"] == other_uri
    assert await a.public_store.is_stored(other_uri) is False


async def test_a_received_foreign_activity_is_cached_with_its_firm_properties(
    service: ActivityPubService, remote_identity: StubIdentity
):
    """A peer's ``firm:`` terms are the peer's claims, and the remote cache keeps them."""
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
    assert cached[FIRM_NS.collections.value] == "http://remote.test/collections"
    assert cached["object"] == "http://remote.test/note/11"

    note = await context.dereference("http://remote.test/note/11")
    assert note is not None
    assert note[FIRM_NS.blockedActor.value] == "http://remote.test/user2"


async def test_an_inbox_handler_sees_the_activity_with_its_firm_properties(tenant: Tenant):
    """What ``process_inbox`` acts on is what was sent: nothing rewrites the document."""
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
    assert seen[FIRM_NS.role.value] == "admin", "the handler sees what the sender sent"
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


# --- Shared inbox (#35) -----------------------------------------------------------


async def _inbox_of(actor_uri: str, store: ResourceStore) -> list[object]:
    """The item list of the actor's personal inbox collection."""
    inbox = await store.get(f"{actor_uri}/inbox")
    assert inbox is not None, f"no inbox stored for {actor_uri}"
    items = inbox["items"]
    assert isinstance(items, list)
    return items


async def _setup_local_actors(store: ResourceStore, *actor_uris: str) -> None:
    """Give each actor a document and an empty personal inbox collection."""
    for actor_uri in actor_uris:
        await setup_resources(
            store,
            [
                {"id": actor_uri, "type": "Person", "inbox": f"{actor_uri}/inbox"},
                {
                    "id": f"{actor_uri}/inbox",
                    "type": "OrderedCollection",
                    "attributedTo": actor_uri,
                    "items": [],
                },
            ],
        )


async def test_shared_inbox_post_reaches_the_named_local_actor_only(
    service: ActivityPubService, tenant: Tenant, remote_identity: StubIdentity
):
    """A POST to the configured shared inbox lands in the named inbox and no other.

    The path comes from the tenant's endpoint, not from the literal "/shared", so a
    tenant with a different shared_inbox_path is served by the same branch.
    """
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"
    addressed, other = "http://tenant1.test/alice", "http://tenant1.test/bob"
    await _setup_local_actors(tenant.public_store, addressed, other)
    activity: JSONObject = {
        "id": "http://remote.test/activities/1",
        "type": "Create",
        "actor": remote_identity.uri,
        "to": [addressed],
        "object": {"id": "http://remote.test/notes/1", "type": "Note"},
    }

    await service.process_post(
        make_context(tenant, principal=remote_identity),
        StubUrl.parse("http://tenant1.test/hub"),
        activity,
    )

    assert await _inbox_of(addressed, tenant.public_store) == ["http://remote.test/activities/1"]
    assert await _inbox_of(other, tenant.public_store) == []


async def _seed_followable_actor(store, uri: str) -> None:
    """An actor that can be followed: document, inbox, followers and outbox."""
    await setup_resources(
        store,
        [
            {
                "id": uri,
                "type": "Person",
                "inbox": f"{uri}/inbox",
                "outbox": f"{uri}/outbox",
                "followers": f"{uri}/followers",
            },
            {"id": f"{uri}/inbox", "type": "OrderedCollection", "items": []},
            {"id": f"{uri}/followers", "type": "Collection", "totalItems": 0},
            {"id": f"{uri}/outbox", "type": "OrderedCollection", "totalItems": 0},
        ],
    )


async def test_shared_inbox_routes_an_unaddressed_follow_to_its_object(
    service: ActivityPubService, tenant: Tenant, remote_identity: StubIdentity
):
    """A `Follow` names its addressee in `object`; Pixelfed sends nothing else.

    Its `Follow` carries only `@context`, `type`, `actor` and `object`, and the delivery
    job posts to `sharedInbox ?? inbox_url`, so a shared inbox that routes on `to`/`cc`
    alone accepts the follow (202) and hands it to nobody. The object is the addressee.
    """
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"
    target = "http://tenant1.test/alice"
    await _seed_followable_actor(tenant.public_store, target)
    activity: JSONObject = {
        "id": "http://remote.test/follows/1",
        "type": "Follow",
        "actor": remote_identity.uri,
        "object": target,
    }

    await service.process_post(
        make_context(tenant, principal=remote_identity),
        StubUrl.parse("http://tenant1.test/hub"),
        activity,
    )

    assert await _inbox_of(target, tenant.public_store) == ["http://remote.test/follows/1"]
    followers = await tenant.public_store.get(f"{target}/followers")
    assert followers is not None and followers["totalItems"] == 1


async def test_shared_inbox_routes_an_unaddressed_undo_follow_to_its_object(
    service: ActivityPubService, tenant: Tenant, remote_identity: StubIdentity
):
    """Undoing a follow names the followed actor one level down, in `object.object`."""
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"
    target = "http://tenant1.test/alice"
    await _seed_followable_actor(tenant.public_store, target)
    await tenant.public_store.insert_item(f"{target}/followers", remote_identity.uri)
    follow: JSONObject = {
        "id": "http://remote.test/follows/2",
        "type": "Follow",
        "actor": remote_identity.uri,
        "object": target,
    }
    undo: JSONObject = {
        "id": "http://remote.test/undos/2",
        "type": "Undo",
        "actor": remote_identity.uri,
        "object": follow,
    }

    await service.process_post(
        make_context(tenant, principal=remote_identity),
        StubUrl.parse("http://tenant1.test/hub"),
        undo,
    )

    followers = await tenant.public_store.get(f"{target}/followers")
    assert followers is not None and followers["totalItems"] == 0


async def test_shared_inbox_ignores_an_unaddressed_follow_of_a_remote_actor(
    service: ActivityPubService, tenant: Tenant, remote_identity: StubIdentity
):
    """The subject has to be a local actor: following someone else is not ours to route."""
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"
    await _seed_followable_actor(tenant.public_store, "http://tenant1.test/alice")
    activity: JSONObject = {
        "id": "http://remote.test/follows/3",
        "type": "Follow",
        "actor": remote_identity.uri,
        "object": "https://elsewhere.test/users/bob",
    }

    await service.process_post(
        make_context(tenant, principal=remote_identity),
        StubUrl.parse("http://tenant1.test/hub"),
        activity,
    )

    followed = await tenant.public_store.get("http://tenant1.test/alice")
    assert followed is not None
    assert followed.get("totalItems") is None
    inbox = await _inbox_of("http://tenant1.test/alice", tenant.public_store)
    assert inbox == []


async def test_shared_inbox_accepts_an_activity_for_nobody_local(
    service: ActivityPubService, tenant: Tenant, remote_identity: StubIdentity
):
    """An activity that names no local actor is accepted and written nowhere."""
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"
    await _setup_local_actors(tenant.public_store, "http://tenant1.test/alice")
    activity: JSONObject = {
        "id": "http://remote.test/activities/2",
        "type": "Create",
        "actor": remote_identity.uri,
        "to": ["https://remote.test/users/stranger"],
        "object": {"id": "http://remote.test/notes/2", "type": "Note"},
    }

    await service.process_post(
        make_context(tenant, principal=remote_identity),
        StubUrl.parse("http://tenant1.test/hub"),
        activity,
    )

    assert await _inbox_of("http://tenant1.test/alice", tenant.public_store) == []
    assert (
        await tenant.public_store.get("http://tenant1.test/hub") is None
    ), "the shared inbox is a route, not a box that accumulates entries"


async def test_shared_inbox_post_requires_authentication(
    service: ActivityPubService, tenant: Tenant
):
    """An unauthenticated POST is refused before any box is touched."""
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"

    with pytest.raises(NotAuthorizedException):
        await service.process_post(
            make_context(tenant),
            StubUrl.parse("http://tenant1.test/hub"),
            {
                "id": "http://remote.test/activities/3",
                "type": "Create",
                "actor": "http://remote.test/user1",
            },
        )


async def test_shared_inbox_repost_writes_one_item(
    service: ActivityPubService, tenant: Tenant, remote_identity: StubIdentity
):
    """Redelivery of the same activity adds no second entry to the box."""
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"
    addressed = "http://tenant1.test/alice"
    await _setup_local_actors(tenant.public_store, addressed)

    def activity() -> JSONObject:
        # A fresh document per POST: applying a Create rewrites the activity's object.
        return {
            "id": "http://remote.test/activities/4",
            "type": "Create",
            "actor": remote_identity.uri,
            "to": [addressed],
            "object": {"id": "http://remote.test/notes/4", "type": "Note"},
        }

    context = make_context(tenant, principal=remote_identity)
    await service.process_post(context, StubUrl.parse("http://tenant1.test/hub"), activity())
    await service.process_post(context, StubUrl.parse("http://tenant1.test/hub"), activity())

    assert await _inbox_of(addressed, tenant.public_store) == ["http://remote.test/activities/4"]


async def test_shared_inbox_forwards_to_local_addressees_once(
    recording_service: tuple[ActivityPubService, StubDeliveryService],
    tenant: Tenant,
    remote_identity: StubIdentity,
):
    """Two local boxes, one activity: our addressees are handed it once, not twice."""
    service, delivery = recording_service
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"
    alice, bob = "http://tenant1.test/alice", "http://tenant1.test/bob"
    note = "http://tenant1.test/notes/1"
    await _setup_local_actors(tenant.public_store, alice, bob)
    await setup_resources(
        tenant.public_store,
        [
            {
                "id": alice,
                "type": "Person",
                "inbox": f"{alice}/inbox",
                "followers": f"{alice}/followers",
            },
            {
                "id": f"{alice}/followers",
                "type": "Collection",
                "attributedTo": alice,
                "items": [bob],
            },
            {"id": note, "type": "Note", "attributedTo": alice},
        ],
    )
    activity: JSONObject = {
        "id": "http://remote.test/activities/5",
        "type": "Create",
        "actor": remote_identity.uri,
        "to": [alice, bob],
        "cc": [f"{alice}/followers"],
        "inReplyTo": note,
        "object": {"id": "http://remote.test/notes/5", "type": "Note", "inReplyTo": note},
    }

    await service.process_post(
        make_context(tenant, principal=remote_identity),
        StubUrl.parse("http://tenant1.test/hub"),
        activity,
    )

    assert delivery.delivered == [
        "http://remote.test/activities/5"
    ], "forwarded once per activity, not once per box"


# Rule 3: a post addressed to the author's followers collection reaches the local
# accounts that follow the author. Relationship, not addressing, decides the delivery.

AUTHOR_FOLLOWERS = "http://remote.test/audience/nonstandard-path"
PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


async def _follower(store: ResourceStore, actor_uri: str, *follows: str) -> None:
    """A local actor with an inbox and a following collection holding ``follows``."""
    await setup_resources(
        store,
        [
            {
                "id": actor_uri,
                "type": "Person",
                "inbox": f"{actor_uri}/inbox",
                "following": f"{actor_uri}/following",
            },
            {
                "id": f"{actor_uri}/inbox",
                "type": "OrderedCollection",
                "attributedTo": actor_uri,
                "items": [],
            },
            {
                "id": f"{actor_uri}/following",
                "type": "OrderedCollection",
                "attributedTo": actor_uri,
                "items": list(follows),
            },
        ],
    )


def audience_post(author: str, *audience: str) -> JSONObject:
    """A ``Create`` carrying ``audience`` on the activity and on its embedded Note."""
    return {
        "id": "http://remote.test/activities/followers-1",
        "type": "Create",
        "actor": author,
        "to": list(audience),
        "object": {
            "id": "http://remote.test/notes/followers-1",
            "type": "Note",
            "to": list(audience),
        },
    }


async def _author_context(
    tenant: Tenant, author: str, followers_uri: str, transport: StubTransport
):
    """A request context whose remote cache already holds the author's document."""
    provider = _provider(transport)
    await provider.remember(
        {
            "id": author,
            "type": "Person",
            "inbox": f"{author}/inbox",
            "followers": followers_uri,
        }
    )
    return make_context(tenant, principal=StubIdentity(author, tenant), remote=provider)


async def test_rule_3_a_public_post_addressed_to_followers_reaches_only_local_followers(
    tenant: Tenant, remote_identity: StubIdentity
):
    """Public plus the author's followers URI: followers get it, others do not."""
    transport = StubTransport()
    context = await _author_context(tenant, remote_identity.uri, AUTHOR_FOLLOWERS, transport)
    delivery = StubDeliveryService()
    await _follower(tenant.public_store, "http://tenant1.test/alice", remote_identity.uri)
    await _follower(tenant.public_store, "http://tenant1.test/bob", "http://remote.test/x")

    recipients = await mastodon_deliver_shared_inbox_activity(
        context, audience_post(remote_identity.uri, AUTHOR_FOLLOWERS, PUBLIC), delivery
    )

    assert recipients == ["http://tenant1.test/alice"]
    assert delivery.recipients == [["http://tenant1.test/alice"]]
    assert delivery.local_only == [True]
    assert transport.urls == [], "the followers collection is never read"


async def test_rule_3_a_followers_only_post_reaches_local_followers(
    tenant: Tenant, remote_identity: StubIdentity
):
    transport = StubTransport()
    context = await _author_context(tenant, remote_identity.uri, AUTHOR_FOLLOWERS, transport)
    await _follower(tenant.public_store, "http://tenant1.test/alice", remote_identity.uri)

    recipients = await mastodon_deliver_shared_inbox_activity(
        context, audience_post(remote_identity.uri, AUTHOR_FOLLOWERS), StubDeliveryService()
    )

    assert recipients == ["http://tenant1.test/alice"]
    assert transport.urls == []


async def test_rule_3_the_followers_uri_is_read_from_the_actor_not_derived_from_its_id(
    tenant: Tenant, remote_identity: StubIdentity
):
    """A nonstandard path is recognised because the author document says so."""
    transport = StubTransport()
    context = await _author_context(tenant, remote_identity.uri, AUTHOR_FOLLOWERS, transport)
    await _follower(tenant.public_store, "http://tenant1.test/alice", remote_identity.uri)

    recipients = await mastodon_deliver_shared_inbox_activity(
        context, audience_post(remote_identity.uri, AUTHOR_FOLLOWERS), StubDeliveryService()
    )

    assert recipients == ["http://tenant1.test/alice"]
    assert AUTHOR_FOLLOWERS != f"{remote_identity.uri}/followers"


async def test_rule_3_an_unrelated_remote_collection_does_not_fan_out(
    tenant: Tenant, remote_identity: StubIdentity
):
    transport = StubTransport()
    context = await _author_context(tenant, remote_identity.uri, AUTHOR_FOLLOWERS, transport)
    await _follower(tenant.public_store, "http://tenant1.test/alice", remote_identity.uri)

    recipients = await mastodon_deliver_shared_inbox_activity(
        context,
        audience_post(remote_identity.uri, "http://remote.test/collections/other"),
        StubDeliveryService(),
    )

    assert recipients == []


async def test_rule_3_public_alone_does_not_reach_every_local_account(
    tenant: Tenant, remote_identity: StubIdentity
):
    """Public is a visibility designation, not a recipient list."""
    transport = StubTransport()
    context = await _author_context(tenant, remote_identity.uri, AUTHOR_FOLLOWERS, transport)
    await _follower(tenant.public_store, "http://tenant1.test/alice")

    recipients = await mastodon_deliver_shared_inbox_activity(
        context, audience_post(remote_identity.uri, PUBLIC), StubDeliveryService()
    )

    assert recipients == []


async def test_rule_3_an_author_without_a_followers_collection_does_not_fan_out(
    tenant: Tenant, remote_identity: StubIdentity
):
    transport = StubTransport()
    provider = _provider(transport)
    await provider.remember({"id": remote_identity.uri, "type": "Person"})
    context = make_context(tenant, principal=remote_identity, remote=provider)
    await _follower(tenant.public_store, "http://tenant1.test/alice", remote_identity.uri)

    recipients = await mastodon_deliver_shared_inbox_activity(
        context,
        audience_post(remote_identity.uri, "http://remote.test/audience/other"),
        StubDeliveryService(),
    )

    assert recipients == []


async def test_rule_3_an_unresolvable_author_does_not_fan_out(
    tenant: Tenant, remote_identity: StubIdentity
):
    """No author document, no relationship the store could check."""
    context = make_context(tenant, principal=remote_identity, remote=_provider(StubTransport()))
    await _follower(tenant.public_store, "http://tenant1.test/alice", remote_identity.uri)

    recipients = await mastodon_deliver_shared_inbox_activity(
        context, audience_post(remote_identity.uri, AUTHOR_FOLLOWERS), StubDeliveryService()
    )

    assert recipients == []


async def test_rule_3_an_activity_signed_by_someone_else_reaches_no_follower(
    tenant: Tenant, remote_identity: StubIdentity
):
    """The signer is the author, as on any other inbox POST."""
    transport = StubTransport()
    provider = _provider(transport)
    await provider.remember(
        {
            "id": remote_identity.uri,
            "type": "Person",
            "inbox": f"{remote_identity.uri}/inbox",
            "followers": AUTHOR_FOLLOWERS,
        }
    )
    await _follower(tenant.public_store, "http://tenant1.test/alice", remote_identity.uri)
    forger = StubIdentity("http://remote.test/forger", tenant)
    context = make_context(tenant, principal=forger, remote=provider)

    recipients = await mastodon_deliver_shared_inbox_activity(
        context, audience_post(remote_identity.uri, AUTHOR_FOLLOWERS), StubDeliveryService()
    )

    assert recipients == []


async def test_rule_3_boxes_the_caller_already_wrote_are_excluded(
    tenant: Tenant, remote_identity: StubIdentity
):
    """Rule 1 delivered to the named owner; the relationship pass must not repeat it."""
    transport = StubTransport()
    context = await _author_context(tenant, remote_identity.uri, AUTHOR_FOLLOWERS, transport)
    alice = "http://tenant1.test/alice"
    await _follower(tenant.public_store, alice, remote_identity.uri)

    recipients = await mastodon_deliver_shared_inbox_activity(
        context,
        audience_post(remote_identity.uri, AUTHOR_FOLLOWERS, PUBLIC, alice),
        StubDeliveryService(),
        exclude=[alice],
    )

    assert recipients == []


async def test_rule_3_a_follow_that_was_rejected_is_not_a_recipient(
    tenant: Tenant, remote_identity: StubIdentity
):
    """Only what the relationship store says: an undone follow is gone from ``following``."""
    transport = StubTransport()
    context = await _author_context(tenant, remote_identity.uri, AUTHOR_FOLLOWERS, transport)
    await _follower(tenant.public_store, "http://tenant1.test/alice")  # follows nobody

    recipients = await mastodon_deliver_shared_inbox_activity(
        context, audience_post(remote_identity.uri, AUTHOR_FOLLOWERS), StubDeliveryService()
    )

    assert recipients == []


async def test_rule_3_a_shared_inbox_post_delivers_to_local_followers(
    service: ActivityPubService, tenant: Tenant, remote_identity: StubIdentity
):
    """The shared-inbox route is what reaches the fan-out; no local actor need be named."""
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"
    transport = StubTransport()
    provider = _provider(transport)
    await provider.remember(
        {
            "id": remote_identity.uri,
            "type": "Person",
            "inbox": f"{remote_identity.uri}/inbox",
            "followers": AUTHOR_FOLLOWERS,
        }
    )
    await _follower(tenant.public_store, "http://tenant1.test/alice", remote_identity.uri)
    delivery = StubDeliveryService()
    service = ActivityPubService(StubAuthorizationService(), delivery)

    await service.process_post(
        make_context(tenant, principal=remote_identity, remote=provider),
        StubUrl.parse("http://tenant1.test/hub"),
        audience_post(remote_identity.uri, AUTHOR_FOLLOWERS, PUBLIC),
    )

    assert delivery.recipients == [["http://tenant1.test/alice"]]


async def test_rule_3_a_named_follower_is_not_fanned_out_a_second_time(
    service: ActivityPubService, tenant: Tenant, remote_identity: StubIdentity
):
    """An addressed local actor who also follows the author is written once, by Rule 1."""
    tenant.endpoints["sharedInbox"] = "http://tenant1.test/hub"
    transport = StubTransport()
    provider = _provider(transport)
    await provider.remember(
        {
            "id": remote_identity.uri,
            "type": "Person",
            "inbox": f"{remote_identity.uri}/inbox",
            "followers": AUTHOR_FOLLOWERS,
        }
    )
    alice = "http://tenant1.test/alice"
    await _follower(tenant.public_store, alice, remote_identity.uri)
    delivery = StubDeliveryService()
    service = ActivityPubService(StubAuthorizationService(), delivery)

    await service.process_post(
        make_context(tenant, principal=remote_identity, remote=provider),
        StubUrl.parse("http://tenant1.test/hub"),
        audience_post(remote_identity.uri, AUTHOR_FOLLOWERS, PUBLIC, alice),
    )

    assert await _inbox_of(alice, tenant.public_store) == [
        audience_post(remote_identity.uri, AUTHOR_FOLLOWERS, PUBLIC, alice)["id"]
    ]
    assert delivery.recipients == [], "Rule 1 already wrote that box"
