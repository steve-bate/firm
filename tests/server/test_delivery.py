"""Outbound delivery: inbox resolution, payload, and the failure model."""

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from firm.core.auth.authorization import CoreAuthorizationService
from firm.core.auth.keys import create_key_pair
from firm.core.delivery.queue import Delivery, DeliveryState
from firm.core.delivery.sqlite import SqliteDeliveryQueue
from firm.core.interfaces import FIRM_NS, APActor, NoOpValidator, Principal, Tenant
from firm.core.remote.cache import MemoryRemoteCache
from firm.core.remote.provider import FetchPolicy, RemoteResourceProvider
from firm.core.resolver import ResourceResolver
from firm.core.services.activitypub.service import post_to_inbox
from firm.core.services.activitypub.support import FirmContext
from firm.core.store.memory import MemoryResourceStore
from firm.server.config import FileStoreConfig, ServerConfig, TenantConfig
from firm.server.delivery import DeliveryError, FirmDeliveryService
from firm.server.delivery_worker import DeliveryWorker
from tests.support import (
    Clock,
    RecordingAuthorizer,
    RecordingQueue,
    StubTransport,
    json_response,
)

PREFIX = "https://local.test"
SENDER = f"{PREFIX}/sender"
RECIPIENT = f"{PREFIX}/recipient"
INBOX = f"{RECIPIENT}/inbox"


@pytest.fixture
def config(tmp_path: Path) -> ServerConfig:
    return ServerConfig([TenantConfig(prefix=PREFIX)], FileStoreConfig(base=tmp_path))


@pytest.fixture
async def tenant(tmp_path: Path) -> Tenant:
    """One tenant holding a signer actor and a local recipient with an inbox."""
    tenant = Tenant(PREFIX, MemoryResourceStore(), MemoryResourceStore(), tmp_path)
    key_pair = create_key_pair()
    await tenant.public_store.put(
        {
            "id": SENDER,
            "type": "Person",
            "publicKey": {
                "id": f"{SENDER}#key",
                "owner": SENDER,
                "publicKeyPem": key_pair.public,
            },
        }
    )
    await tenant.private_store.put(
        {
            "id": f"{SENDER}/credentials",
            "type": FIRM_NS.Credentials.value,
            "attributedTo": SENDER,
            FIRM_NS.privateKey.value: key_pair.private,
        }
    )
    await tenant.public_store.put({"id": RECIPIENT, "type": "Person", "inbox": INBOX})
    await tenant.public_store.put(
        {"id": INBOX, "type": "OrderedCollection", "attributedTo": RECIPIENT, "items": []}
    )
    return tenant


REMOTE_ACTOR = "https://remote.test/users/bob"
REMOTE_ACTOR_DOC = {"id": REMOTE_ACTOR, "type": "Person", "inbox": f"{REMOTE_ACTOR}/inbox"}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """A delivery test fetches remote.test; the SSRF guard resolves it, so name an address."""
    from firm.core.remote import provider as provider_module

    monkeypatch.setattr(
        provider_module, "_resolve_host", lambda host, port: ["93.184.216.34"], raising=False
    )


def make_resolver(tenant: Tenant, transport: StubTransport) -> ResourceResolver:
    """The real resolver over one tenant and a fetch provider backed by the stub."""
    provider = RemoteResourceProvider(MemoryRemoteCache(), transport, policy=FetchPolicy())
    return ResourceResolver({str(tenant.prefix): tenant}, provider)


@pytest.fixture
def transport() -> StubTransport:
    return StubTransport()


@pytest.fixture
def queue(tmp_path: Path) -> RecordingQueue:
    return RecordingQueue()


@pytest.fixture
def service(config: ServerConfig) -> FirmDeliveryService:
    return FirmDeliveryService(config)


async def _local_recipient(tenant: Tenant, owner_is_publisher: bool) -> tuple[Tenant, str]:
    """The publisher's own inbox, or a second tenant's — the same record either way."""
    if owner_is_publisher:
        return tenant, INBOX
    other = Tenant("https://other.test", MemoryResourceStore(), MemoryResourceStore(), Path())
    await other.public_store.put(
        {"id": "https://other.test/u2", "type": "Person", "inbox": "https://other.test/u2/inbox"}
    )
    await other.public_store.put(
        {
            "id": "https://other.test/u2/inbox",
            "type": "OrderedCollection",
            "attributedTo": "https://other.test/u2",
            "items": [],
        }
    )
    return other, "https://other.test/u2/inbox"


def activity(**overrides: object) -> dict:
    document: dict = {
        "id": f"{PREFIX}/activities/1",
        "type": "Create",
        "actor": SENDER,
        "to": [RECIPIENT],
        "object": {"id": f"{PREFIX}/notes/1", "type": "Note"},
    }
    document.update(overrides)
    return document


async def test_local_delivery_writes_the_inbox_items(tenant, service):
    await service.deliver(tenant, {PREFIX: tenant}, activity())

    inbox = await tenant.public_store.get(INBOX)

    assert inbox["items"] == [f"{PREFIX}/activities/1"]
    assert inbox["totalItems"] == 1


async def test_deliver_reports_the_inbox_it_wrote_to(tenant, service):
    report = await service.deliver(tenant, {PREFIX: tenant}, activity())

    assert report.delivered == (INBOX,)
    assert report.failed == ()


async def test_an_activity_without_an_actor_is_reported_not_raised(tenant, service):
    report = await service.deliver(tenant, {PREFIX: tenant}, activity(actor=None))

    assert report.delivered == ()
    assert report.failed[0].inbox is None
    assert "actor" in report.failed[0].reason


async def test_a_local_delivery_needs_no_credentials(tenant, service):
    tenant.private_store = MemoryResourceStore()

    report = await service.deliver(tenant, {PREFIX: tenant}, activity())

    assert report.delivered == (INBOX,), "nothing is signed on the way into our own store"


async def test_a_remote_delivery_without_credentials_is_abandoned(tenant, config, transport, queue):
    tenant.private_store = MemoryResourceStore()
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    service = FirmDeliveryService(config, transport, queue=queue)
    service.set_resolver(make_resolver(tenant, transport))

    report = await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [REMOTE_ACTOR]}))

    assert report.delivered == ()
    assert "credentials" in report.failed[0].reason
    rows = await queue.list()
    assert [row.state for row in rows] == [DeliveryState.ABANDONED], "a key is not coming"


async def test_delivering_the_same_activity_twice_does_not_duplicate_the_item(tenant, service):
    await service.deliver(tenant, {PREFIX: tenant}, activity())
    await service.deliver(tenant, {PREFIX: tenant}, activity())

    inbox = await tenant.public_store.get(INBOX)

    assert inbox["items"] == [f"{PREFIX}/activities/1"]
    assert inbox["totalItems"] == 1


async def test_a_local_create_stores_its_object_like_a_post_does(tenant, service):
    await service.deliver(tenant, {PREFIX: tenant}, activity())

    assert await tenant.public_store.get(f"{PREFIX}/notes/1") is not None


async def test_serialize_survives_a_firm_prefixed_property(tenant, service):
    document = activity(**{FIRM_NS.collections.value: f"{PREFIX}/collections"})

    message = await service._serialize(tenant, document)

    assert FIRM_NS.collections.value not in message


async def test_serialize_strips_firm_properties_from_the_embedded_object(tenant, service):
    document = activity(
        object={
            "id": f"{PREFIX}/notes/1",
            "type": "Note",
            FIRM_NS.privateKey.value: "leak",
        }
    )

    message = await service._serialize(tenant, document)

    assert message["object"] == {"id": f"{PREFIX}/notes/1", "type": "Note"}
    assert document["object"][FIRM_NS.privateKey.value] == "leak", "the input is untouched"


REMOTE_INBOX = "https://remote.test/users/bob/inbox"


async def _no_sleep(seconds: float) -> None:
    return None


class Sleeps:
    """Records the backoff delays instead of waiting them out."""

    def __init__(self) -> None:
        self.values: list[float] = []

    async def record(self, seconds: float) -> None:
        self.values.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


async def test_post_uses_the_transport_with_the_configured_timeout(tenant, config, transport):
    service = FirmDeliveryService(config, transport)
    auth = await service._signer(tenant, SENDER)
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=202))

    await service._post(REMOTE_INBOX, {"id": f"{PREFIX}/activities/1"}, auth)

    post = transport.posts[0]
    assert post["url"] == REMOTE_INBOX
    assert post["json"] == {"id": f"{PREFIX}/activities/1"}
    assert post["content_type"] == "application/activity+json"
    assert post["timeout"] == config.delivery.timeout


async def test_a_5xx_is_retried_until_it_succeeds(tenant, config, transport, sleeps):
    transport.post_script(
        REMOTE_INBOX, [json_response({}, status_code=503), json_response({}, status_code=202)]
    )
    service = FirmDeliveryService(config, transport, sleep=sleeps.record)
    auth = await service._signer(tenant, SENDER)

    await service._post(REMOTE_INBOX, {"id": "x"}, auth)

    assert len(transport.posts) == 2
    assert sleeps.values == [0.5], "one wait between the two attempts"


async def test_backoff_doubles_between_attempts(tenant, config, transport, sleeps):
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=500))
    service = FirmDeliveryService(config, transport, sleep=sleeps.record)
    auth = await service._signer(tenant, SENDER)

    with pytest.raises(DeliveryError) as raised:
        await service._post(REMOTE_INBOX, {"id": "x"}, auth)

    assert sleeps.values == [0.5, 1.0]
    assert len(transport.posts) == 3
    assert raised.value.attempts == 3
    assert raised.value.reason == "HTTP 500"
    assert raised.value.status == 500


async def test_a_4xx_is_not_retried(tenant, config, transport, sleeps):
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=404))
    service = FirmDeliveryService(config, transport, sleep=sleeps.record)
    auth = await service._signer(tenant, SENDER)

    with pytest.raises(DeliveryError) as raised:
        await service._post(REMOTE_INBOX, {"id": "x"}, auth)

    assert len(transport.posts) == 1
    assert sleeps.values == []
    assert raised.value.attempts == 1
    assert raised.value.status == 404


async def test_a_transport_error_is_retried(tenant, config):
    transport = StubTransport()  # an unscripted POST raises
    service = FirmDeliveryService(config, transport, sleep=_no_sleep)
    auth = await service._signer(tenant, SENDER)

    with pytest.raises(DeliveryError) as raised:
        await service._post(REMOTE_INBOX, {"id": "x"}, auth)

    assert len(transport.posts) == 3
    assert "AssertionError" in raised.value.reason


async def test_a_remote_follower_resolves_through_the_resolver(tenant, config, transport):
    followers = f"{SENDER}/followers"
    await tenant.public_store.put(
        {"id": followers, "type": "Collection", "attributedTo": SENDER, "items": [REMOTE_ACTOR]}
    )
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(f"{REMOTE_ACTOR}/inbox", json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport)
    service.set_resolver(make_resolver(tenant, transport))

    report = await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [followers]}))

    assert report.delivered == (f"{REMOTE_ACTOR}/inbox",)
    assert transport.urls == [REMOTE_ACTOR], "the actor document was fetched once"


async def test_a_cyclic_collection_terminates(tenant, config):
    loop = f"{PREFIX}/loop"
    await tenant.public_store.put(
        {"id": loop, "type": "Collection", "attributedTo": SENDER, "items": [loop]}
    )
    service = FirmDeliveryService(config)

    report = await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [loop]}))

    assert report.delivered == ()


async def test_expansion_stops_at_the_configured_depth(tenant, config):
    outer, inner, deeper = (f"{PREFIX}/{name}" for name in ("outer", "inner", "deeper"))
    for uri, item in ((outer, inner), (inner, deeper), (deeper, RECIPIENT)):
        await tenant.public_store.put(
            {"id": uri, "type": "Collection", "attributedTo": SENDER, "items": [item]}
        )
    service = FirmDeliveryService(config)  # max_expand_depth is 2

    report = await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [outer]}))

    assert report.delivered == (), "the walk stopped before reaching the actor"


async def test_another_local_tenant_recipient_is_resolved_from_its_own_store(tenant, config):
    other = Tenant("https://other.test", MemoryResourceStore(), MemoryResourceStore(), Path())
    await other.public_store.put(
        {"id": "https://other.test/u2", "type": "Person", "inbox": "https://other.test/u2/inbox"}
    )
    service = FirmDeliveryService(config)
    service.set_resolver(ResourceResolver({PREFIX: tenant, "https://other.test": other}))

    inboxes = await service._resolve_inboxes(tenant, {"https://other.test/u2"})

    assert inboxes == {"https://other.test/u2/inbox"}


async def test_audience_is_a_target_field(tenant, config):
    document = activity(**{"to": [], "audience": [RECIPIENT]})
    service = FirmDeliveryService(config)

    report = await service.deliver(tenant, {PREFIX: tenant}, document)

    assert report.delivered == (INBOX,), "an audience-only activity still reaches its addressee"


async def test_an_actor_is_not_delivered_its_own_activity(tenant, config):
    sender = await tenant.public_store.get(SENDER)
    await tenant.public_store.put({**sender, "inbox": f"{SENDER}/inbox"})
    await tenant.public_store.put(
        {"id": f"{SENDER}/inbox", "type": "OrderedCollection", "attributedTo": SENDER, "items": []}
    )
    document = activity(**{"to": [SENDER, RECIPIENT]})
    service = FirmDeliveryService(config)

    report = await service.deliver(tenant, {PREFIX: tenant}, document)

    assert report.delivered == (INBOX,), "the publisher is excluded from its own targets"
    assert (await tenant.public_store.get(f"{SENDER}/inbox"))["items"] == []


async def test_shared_inbox_is_read_from_endpoints(tenant, config, transport):
    transport.script(
        REMOTE_ACTOR,
        json_response(
            {
                "id": REMOTE_ACTOR,
                "type": "Person",
                "inbox": f"{REMOTE_ACTOR}/inbox",
                "endpoints": {"sharedInbox": "https://remote.test/shared"},
            }
        ),
    )
    transport.post_script("https://remote.test/shared", json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport)
    service.set_resolver(make_resolver(tenant, transport))

    report = await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [REMOTE_ACTOR]}))

    assert report.delivered == ("https://remote.test/shared",)


async def test_a_local_recipient_is_delivered_to_its_personal_inbox(tenant, config, transport):
    await tenant.public_store.put(
        {
            "id": RECIPIENT,
            "type": "Person",
            "inbox": INBOX,
            "endpoints": {"sharedInbox": f"{PREFIX}/shared"},
        }
    )
    service = FirmDeliveryService(config, transport)

    report = await service.deliver(tenant, {PREFIX: tenant}, activity())

    assert report.delivered == (INBOX,)


async def test_a_recipient_without_an_inbox_is_skipped_and_logged(
    tenant, config, transport, caplog
):
    await tenant.public_store.put({"id": RECIPIENT, "type": "Person"})
    service = FirmDeliveryService(config, transport)

    with caplog.at_level("WARNING", logger="firm.server.delivery"):
        report = await service.deliver(tenant, {PREFIX: tenant}, activity())

    assert report.delivered == ()
    assert RECIPIENT in caplog.text, "the actor is named, so the bad document can be found"


def pending(**overrides: Any) -> Delivery:
    """A unit owed by the local tenant to one remote inbox, due at t=1000."""
    return replace(
        Delivery(
            id="d1",
            tenant=PREFIX,
            activity=f"{PREFIX}/activities/1",
            endpoint=REMOTE_INBOX,
            created_at=1_000.0,
            next_attempt_at=1_000.0,
        ),
        **overrides,
    )


def worker_for(service, queue, config, clock) -> DeliveryWorker:
    return DeliveryWorker(service, queue, config.delivery, clock=clock)


async def test_tick_posts_a_due_delivery(tenant, config, transport, queue):
    await tenant.public_store.put(activity(**{"to": [REMOTE_ACTOR]}))
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport, queue=queue)
    service.set_resolver(make_resolver(tenant, transport))
    worker = worker_for(service, queue, config, Clock(1_000.0))
    await queue.enqueue(pending())

    assert await worker.tick() == 1
    row = await queue.get("d1")
    assert row is not None and row.state is DeliveryState.DELIVERED
    assert [post["url"] for post in transport.posts] == [REMOTE_INBOX]


async def test_tick_leaves_a_delivery_that_is_not_yet_due(tenant, config, transport, queue):
    service = FirmDeliveryService(config, transport, queue=queue)
    worker = worker_for(service, queue, config, Clock(1_000.0))
    await queue.enqueue(pending(next_attempt_at=2_000.0))

    assert await worker.tick() == 0
    row = await queue.get("d1")
    assert row is not None and row.state is DeliveryState.PENDING
    assert transport.posts == []


async def test_tick_reclaims_a_stale_claim_and_posts_it(tenant, config, transport, queue):
    await tenant.public_store.put(activity(**{"to": [REMOTE_ACTOR]}))
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport, queue=queue)
    service.set_resolver(make_resolver(tenant, transport))
    clock = Clock(1_000.0)
    worker = worker_for(service, queue, config, clock)
    await queue.enqueue(pending())
    await queue.claim_due(now=1_000.0, limit=10)  # a worker that then died
    clock.now = 1_400.0

    assert await worker.tick() == 1
    row = await queue.get("d1")
    assert row is not None and row.state is DeliveryState.DELIVERED
    assert len(transport.posts) == 1


async def test_tick_abandons_a_delivery_older_than_max_age(tenant, config, transport, queue):
    await tenant.public_store.put(activity(**{"to": [REMOTE_ACTOR]}))
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=503))
    service = FirmDeliveryService(config, transport, queue=queue, sleep=_no_sleep)
    service.set_resolver(make_resolver(tenant, transport))
    worker = worker_for(service, queue, config, Clock(100_000.0))
    await queue.enqueue(pending(created_at=0.0))

    assert await worker.tick() == 1
    row = await queue.get("d1")
    assert row is not None and row.state is DeliveryState.ABANDONED
    assert row.last_reason == "HTTP 503"


async def test_tick_prunes_delivered_history(tenant, config, transport):
    queue = RecordingQueue(clock=Clock(1_000.0))
    await tenant.public_store.put(activity(**{"to": [REMOTE_ACTOR]}))
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport, queue=queue)
    service.set_resolver(make_resolver(tenant, transport))
    await queue.enqueue(pending())
    await service.run(pending(), await tenant.public_store.get(f"{PREFIX}/activities/1"))
    worker = worker_for(service, queue, config, Clock(10_000.0))

    await worker.tick()

    assert await queue.get("d1") is None, "the delivered row is history, and it expired"


async def test_a_pending_delivery_survives_a_restart(tenant, config, transport, tmp_path):
    path = tmp_path / "delivery.sqlite"
    first = SqliteDeliveryQueue(path)
    await first.enqueue(pending())
    await first.close()

    await tenant.public_store.put(activity(**{"to": [REMOTE_ACTOR]}))
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=202))
    second = SqliteDeliveryQueue(path)
    service = FirmDeliveryService(config, transport, queue=second)
    service.set_resolver(make_resolver(tenant, transport))
    worker = worker_for(service, second, config, Clock(1_000.0))

    assert await worker.tick() == 1
    assert len(transport.posts) == 1, "the unit outlived the process that recorded it"
    assert (await second.get("d1")).state is DeliveryState.DELIVERED
    await second.close()


async def test_every_endpoint_is_enqueued_before_it_is_posted(tenant, config, queue):
    transport = StubTransport(calls=queue.calls)
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport, queue=queue)
    service.set_resolver(make_resolver(tenant, transport))

    await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [REMOTE_ACTOR]}))

    assert queue.calls.index(("enqueue", REMOTE_INBOX)) < queue.calls.index(
        ("post", REMOTE_INBOX)
    ), "the unit is recorded before the request leaves"


async def test_a_delivered_endpoint_is_marked_delivered(tenant, config, transport, queue):
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport, queue=queue)
    service.set_resolver(make_resolver(tenant, transport))

    await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [REMOTE_ACTOR]}))

    rows = await queue.list()
    assert [row.state for row in rows] == [DeliveryState.DELIVERED]
    assert rows[0].endpoint == REMOTE_INBOX
    assert rows[0].tenant == PREFIX


async def test_a_faulting_endpoint_is_rescheduled_with_backoff(tenant, config, transport, queue):
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=503))
    service = FirmDeliveryService(config, transport, queue=queue, sleep=_no_sleep)
    service.set_resolver(make_resolver(tenant, transport))

    report = await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [REMOTE_ACTOR]}))

    rows = await queue.list()
    assert rows[0].state is DeliveryState.PENDING
    assert rows[0].attempts == 1
    assert rows[0].next_attempt_at > rows[0].created_at
    assert rows[0].last_reason == "HTTP 503"
    assert [failure.inbox for failure in report.failed] == [REMOTE_INBOX]


async def test_a_refused_endpoint_is_abandoned_not_rescheduled(tenant, config, transport, queue):
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(REMOTE_INBOX, json_response({}, status_code=403))
    service = FirmDeliveryService(config, transport, queue=queue, sleep=_no_sleep)
    service.set_resolver(make_resolver(tenant, transport))

    await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [REMOTE_ACTOR]}))

    rows = await queue.list()
    assert rows[0].state is DeliveryState.ABANDONED
    assert rows[0].last_reason == "HTTP 403"
    assert rows[0].next_attempt_at == rows[0].created_at, "it is not coming back"


@pytest.mark.parametrize("owner_is_publisher", [True, False], ids=["same-tenant", "other-tenant"])
async def test_local_delivery_writes_the_owners_inbox_and_records_it(
    tenant, transport, queue, tmp_path, owner_is_publisher
):
    owner, inbox = await _local_recipient(tenant, owner_is_publisher)
    recipient_uri = RECIPIENT if owner_is_publisher else "https://other.test/u2"
    config = ServerConfig(
        [TenantConfig(prefix=PREFIX), TenantConfig(prefix=str(owner.prefix))],
        FileStoreConfig(base=tmp_path),
    )
    service = FirmDeliveryService(config, transport, queue=queue)

    await service.deliver(
        tenant, {PREFIX: tenant, owner.prefix: owner}, activity(**{"to": [recipient_uri]})
    )

    assert (await owner.public_store.get(inbox))["items"] == [f"{PREFIX}/activities/1"]
    assert transport.posts == [], "no request left the process"
    rows = await queue.list()
    assert [row.endpoint for row in rows] == [inbox]
    assert (
        rows[0].tenant == PREFIX
    ), "the record's tenant is the publisher, not the endpoint's owner"
    assert rows[0].state is DeliveryState.DELIVERED


async def test_delivery_to_a_remote_server_posts_a_signed_activity(
    tenant, config, transport, queue
):
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(f"{REMOTE_ACTOR}/inbox", json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport, queue=queue)
    service.set_resolver(make_resolver(tenant, transport))

    await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [REMOTE_ACTOR]}))

    assert [post["url"] for post in transport.posts] == [f"{REMOTE_ACTOR}/inbox"]
    assert transport.posts[0]["auth"] is not None, "a remote POST is signed by the publishing actor"
    assert transport.urls == [REMOTE_ACTOR], "the recipient document was fetched, not stored"


async def test_a_local_delivery_asks_the_receiving_tenant_to_authorize_the_post(
    tenant, config, queue
):
    authorizer = RecordingAuthorizer()
    service = FirmDeliveryService(config, queue=queue, authorizer=authorizer)

    await service.deliver(tenant, {PREFIX: tenant}, activity())

    assert [call[0].prefix for call in authorizer.post_calls] == [PREFIX], "the owner is asked"
    assert authorizer.post_calls[0][1].uri == SENDER, "the principal is the publishing actor"


async def test_a_denied_post_writes_nothing_and_the_unit_is_abandoned(tenant, config, queue):
    authorizer = RecordingAuthorizer(
        post_authorized=False, reason="inbox post is blocked for actor"
    )
    service = FirmDeliveryService(config, queue=queue, authorizer=authorizer)

    await service.deliver(tenant, {PREFIX: tenant}, activity())

    assert (await tenant.public_store.get(INBOX))["items"] == []
    rows = await queue.list()
    assert [row.state for row in rows] == [DeliveryState.ABANDONED]
    assert "blocked for actor" in rows[0].last_reason


async def test_a_remote_endpoint_asks_no_local_policy(tenant, config, transport, queue):
    authorizer = RecordingAuthorizer()
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    transport.post_script(f"{REMOTE_ACTOR}/inbox", json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport, queue=queue, authorizer=authorizer)
    service.set_resolver(make_resolver(tenant, transport))

    await service.deliver(tenant, {PREFIX: tenant}, activity(**{"to": [REMOTE_ACTOR]}))

    assert authorizer.post_calls == [], "the peer decides at its own inbox"


INBOXES = [f"https://remote.test/users/{name}/inbox" for name in "abc"]


async def test_a_slow_inbox_does_not_delay_the_others(tenant, config):
    transport = StubTransport(delay=0.01)
    for inbox in INBOXES:
        transport.post_script(inbox, json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport)
    report = await service._fan_out(
        tenant, {PREFIX: tenant}, activity(), f"{PREFIX}/activities/1", set(INBOXES)
    )

    assert sorted(report.delivered) == sorted(INBOXES)
    assert transport.max_concurrent_posts > 1, "the inboxes were in flight together"


async def test_a_failed_inbox_is_reported_beside_a_successful_one(tenant, config):
    transport = StubTransport()
    transport.post_script(INBOXES[0], json_response({}, status_code=202))
    transport.post_script(INBOXES[1], json_response({}, status_code=404))
    transport.post_script(INBOXES[2], json_response({}, status_code=202))
    service = FirmDeliveryService(config, transport, sleep=_no_sleep)
    report = await service._fan_out(
        tenant, {PREFIX: tenant}, activity(), f"{PREFIX}/activities/1", set(INBOXES)
    )

    assert sorted(report.delivered) == sorted([INBOXES[0], INBOXES[2]])
    assert [(failure.inbox, failure.attempts) for failure in report.failed] == [(INBOXES[1], 1)]


async def test_serialize_adds_the_activitystreams_context(tenant, service):
    message = await service._serialize(tenant, activity())

    assert message["@context"] == "https://www.w3.org/ns/activitystreams"


async def test_serialize_embeds_a_local_object(tenant, service):
    await tenant.public_store.put({"id": f"{PREFIX}/notes/1", "type": "Note", "content": "hi"})

    message = await service._serialize(tenant, activity(object=f"{PREFIX}/notes/1"))

    assert message["object"]["content"] == "hi"


async def test_serialize_leaves_a_foreign_object_as_a_uri(tenant, service):
    foreign = "https://remote.test/notes/9"
    await tenant.public_store.put({"id": foreign, "type": "Note", "content": "not ours"})

    message = await service._serialize(tenant, activity(object=foreign))

    assert message["object"] == foreign


async def test_serialize_leaves_a_follow_object_as_a_uri(tenant, service):
    await tenant.public_store.put(
        {"id": f"{PREFIX}/recipient2", "type": "Person", "inbox": f"{PREFIX}/recipient2/inbox"}
    )
    document = activity(type="Follow", object=f"{PREFIX}/recipient2")

    message = await service._serialize(tenant, document)

    assert message["object"] == f"{PREFIX}/recipient2"


async def test_a_local_follow_is_answered_with_an_accept(tenant, service):
    followers = f"{RECIPIENT}/followers"
    outbox = f"{RECIPIENT}/outbox"
    recipient = await tenant.public_store.get(RECIPIENT)
    await tenant.public_store.put({**recipient, "followers": followers, "outbox": outbox})
    await tenant.public_store.put(
        {"id": followers, "type": "Collection", "attributedTo": RECIPIENT, "items": []}
    )
    await tenant.public_store.put(
        {"id": outbox, "type": "OrderedCollection", "attributedTo": RECIPIENT, "items": []}
    )
    following = f"{SENDER}/following"
    sender = await tenant.public_store.get(SENDER)
    await tenant.public_store.put({**sender, "inbox": f"{SENDER}/inbox", "following": following})
    await tenant.public_store.put(
        {"id": f"{SENDER}/inbox", "type": "OrderedCollection", "attributedTo": SENDER, "items": []}
    )
    await tenant.public_store.put(
        {"id": following, "type": "Collection", "attributedTo": SENDER, "items": []}
    )
    follow = {
        "id": f"{PREFIX}/activities/follow",
        "type": "Follow",
        "actor": SENDER,
        "object": RECIPIENT,
        "to": [RECIPIENT],
    }

    await service.deliver(tenant, {PREFIX: tenant}, follow)

    assert SENDER in (await tenant.public_store.get(followers))["items"]
    accepts = [
        item for item in (await tenant.public_store.get(outbox))["items"] if "/accept/" in item
    ]
    assert accepts, "the follow was accepted, which an append would not do"


async def test_a_forwarded_reply_reaches_another_local_follower(tenant, config, transport):
    """AP 7.1.2 end to end: a remote reply to our followers reaches them, and no peer."""
    owner = RECIPIENT
    other = f"{PREFIX}/other"
    other_inbox = f"{other}/inbox"
    followers = f"{SENDER}/followers"
    note = f"{PREFIX}/notes/1"
    await tenant.public_store.put({"id": other, "type": "Person", "inbox": other_inbox})
    await tenant.public_store.put(
        {
            "id": other_inbox,
            "type": "OrderedCollection",
            "attributedTo": other,
            "items": [],
        }
    )
    await tenant.public_store.put(
        {
            "id": followers,
            "type": "Collection",
            "attributedTo": SENDER,
            "items": [other, REMOTE_ACTOR],
        }
    )
    await tenant.public_store.put({"id": note, "type": "Note", "attributedTo": SENDER})
    transport.script(REMOTE_ACTOR, json_response(REMOTE_ACTOR_DOC))
    service = FirmDeliveryService(config, transport)
    service.set_resolver(make_resolver(tenant, transport))
    authorizer = CoreAuthorizationService()
    remote_actor = {"id": REMOTE_ACTOR, "type": "Person", "inbox": f"{REMOTE_ACTOR}/inbox"}
    context = FirmContext(
        service.resolver, tenant, Principal(cast(APActor, remote_actor), tenant), authorizer
    )
    reply = {
        "id": "https://remote.test/activities/7",
        "type": "Create",
        "actor": REMOTE_ACTOR,
        "to": [followers],
        "inReplyTo": note,
        "object": {"id": "https://remote.test/notes/7", "type": "Note", "inReplyTo": note},
    }

    await post_to_inbox(
        context,
        cast(APActor, await tenant.public_store.get(owner)),
        reply,
        box_uri=INBOX,
        validator=NoOpValidator(),
        delivery_service=service,
    )

    assert (await tenant.public_store.get(other_inbox))["items"] == [
        "https://remote.test/activities/7"
    ], "the other local follower saw the reply"
    assert transport.posts == [], "forwarding does not re-post to the peer's followers"
