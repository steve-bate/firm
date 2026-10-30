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
    await tenant.public_store.put({"id": INBOX, "type": "OrderedCollection", "items": []})
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
