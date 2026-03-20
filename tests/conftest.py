import asyncio
from typing import cast

import pytest
from fastapi import Request
from starlette.testclient import TestClient

from firm.core.interfaces import APActor, Principal, Tenant
from firm.server.auth import get_principal
from firm.server.config import MemoryStoreConfig, ServerConfig, TenantConfig
from firm.server.server import app_factory, clear_app

TENANT_PREFIX = "http://tenant1.test"


@pytest.fixture
def client(tmp_path):
    with TestClient(
        app_factory(
            ServerConfig(
                tenants=[TenantConfig(prefix=TENANT_PREFIX)],
                store=MemoryStoreConfig(files=tmp_path),
            )
        ),
        base_url=TENANT_PREFIX,
    ) as c:
        yield c
        clear_app()


class LazyActor(dict):
    def __init__(self, tenant: Tenant, **kwargs):
        self._tenant = tenant
        super().__init__(**kwargs)

    def __getitem__(self, item):
        if value := super().get(item):
            return value
        if item == "outbox":
            outbox_id = f"{self['id']}/outbox"
            asyncio.get_event_loop().run_until_complete(  # Check if outbox already exists
                self._tenant.public_store.put(
                    {
                        "id": outbox_id,
                        "type": "OrderedCollection",
                        "attributedTo": self["id"],
                        "totalItems": 0,
                        "items": [],
                    }
                )
            )
            self["outbox"] = outbox_id
            return outbox_id
        raise AttributeError(f"{self.__class__.__name__} has no attribute {item}")


@pytest.fixture
async def actor(client: TestClient):
    """Yields the actor ID and installs a get_principal override that authenticates as that actor."""

    tenant = client.app.state.tenants[TENANT_PREFIX]
    actor_id = f"{tenant.prefix}/actor1"

    actor_doc = LazyActor(
        tenant,
        id=actor_id,
        type="Person",
        inbox=f"{actor_id}/inbox",
        # outbox=f"{actor_id}/outbox",
        followers=f"{actor_id}/followers",
        following=f"{actor_id}/following",
        likes=f"{actor_id}/likes",
    )

    async def _get_principal(request: Request) -> Principal:
        ap_actor = actor_doc
        return Principal(cast(APActor, ap_actor), request.state.tenant)

    client.app.dependency_overrides[get_principal] = _get_principal
    await tenant.public_store.put(actor_doc)
    yield actor_doc
    client.app.dependency_overrides.pop(get_principal, None)
