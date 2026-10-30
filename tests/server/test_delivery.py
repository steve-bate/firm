"""Outbound delivery: inbox resolution, payload, and the failure model."""

from pathlib import Path

import pytest

from firm.core.auth.keys import create_key_pair
from firm.core.interfaces import FIRM_NS, Tenant
from firm.core.store.memory import MemoryResourceStore
from firm.server.config import FileStoreConfig, ServerConfig, TenantConfig
from firm.server.delivery import FirmDeliveryService

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


@pytest.fixture
def service(config: ServerConfig) -> FirmDeliveryService:
    return FirmDeliveryService(config)


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


async def test_missing_credentials_are_reported_not_raised(tenant, service):
    tenant.private_store = MemoryResourceStore()

    report = await service.deliver(tenant, {PREFIX: tenant}, activity())

    assert report.delivered == ()
    assert "credentials" in report.failed[0].reason


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
    sender = await tenant.public_store.get(SENDER)
    await tenant.public_store.put({**sender, "inbox": f"{SENDER}/inbox"})
    await tenant.public_store.put(
        {"id": f"{SENDER}/inbox", "type": "OrderedCollection", "attributedTo": SENDER, "items": []}
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
