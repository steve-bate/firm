import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Mapping, overload

from firm.core.interfaces import (
    APActor,
    AuthorizationService,
    DeliveryService,
    Identity,
    JSONObject,
    Tenant,
)
from firm.core.resolver import ResourceResolver
from firm.core.services.activitypub.exceptions import NotFoundException
from firm.core.services.activitypub.publish import outbox_send
from firm.core.util import get_id, get_types, is_local_uri, resource_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FirmContext:
    """Per-request identity: who is asking, for which tenant, and what may be read.

    This is where a cross-instance decision is made, because this is the layer that
    holds the principal. The resolver only says which instance owns a URI, a store
    only returns documents, and the authorization service only decides.
    """

    resolver: ResourceResolver
    tenant: Tenant
    principal: Identity | None
    authorizer: AuthorizationService

    @property
    def tenants(self) -> Mapping[str, Tenant]:
        """The instance registry, for addressing rather than for reads."""
        return self.resolver.tenants

    @overload
    async def dereference(self, resource_id: Any, *, required: Literal[True]) -> JSONObject: ...

    @overload
    async def dereference(
        self, resource_id: Any, *, required: Literal[False]
    ) -> JSONObject | None: ...

    @overload
    async def dereference(self, resource_id: Any) -> JSONObject | None: ...

    async def dereference(self, resource_id: Any, *, required: bool = False) -> JSONObject | None:
        if isinstance(resource_id, dict):
            resource_id = get_id(resource_id)
        if not resource_id:
            if required:
                raise ValueError("Resource ID is required")
            return None

        resource = await self._resolve(str(resource_id))

        if required and resource is None:
            raise NotFoundException(f"Resource {resource_id} not found")
        return resource

    async def _resolve(self, uri: str) -> JSONObject | None:
        if is_local_uri(self.tenant, uri):
            return await self.tenant.public_store.get(uri)

        owner = self.resolver.owner(uri)
        if owner is None:
            return await self.resolver.fetch(uri)

        document = await owner.public_store.get(uri)
        if document is None:
            return None
        decision = await self.authorizer.is_get_authorized(owner, self.principal, document)
        if not decision.authorized:
            log.info(
                "Cross-instance read denied by %s: %s (%s)", owner.prefix, uri, decision.reason
            )
            return None
        return document

    async def keep(self, document: JSONObject) -> None:
        """Keep a document that arrived from another server.

        A document under this tenant's prefix is ours to hold, so it goes to the
        tenant's own store. Anything else belongs to the server that authored it and is
        remembered in the remote cache; a tenant store never receives it.
        """
        uri = get_id(document) or ""
        if not uri:
            log.debug("Not keeping a document without an id")
            return
        if is_local_uri(self.tenant, uri):
            await self.tenant.public_store.put(document)
            return
        if self.resolver.owner(uri) is not None:
            log.warning("Not keeping a document owned by another tenant: %s", uri)
            return
        await self.resolver.remember(document)


@dataclass(frozen=True)
class FirmBoxContext(FirmContext):
    """What box processing needs on top of the base context: the box, its owner,
    and how to publish as that owner."""

    box_owner: APActor
    box_uri: str
    delivery_service: DeliveryService

    async def send(self, activity: JSONObject) -> None:
        """Publish as the box owner, to the owner's outbox - never to ``box_uri``,
        which for an inbox is the inbox the activity arrived at."""
        outbox_uri = resource_id(self.box_owner.get("outbox"))
        if not outbox_uri:
            raise NotImplementedError("Box owner has no outbox")
        await outbox_send(self.tenant, self.tenants, activity, outbox_uri, self.delivery_service)


def generate_id(actor: APActor | JSONObject | str, subpath: str | JSONObject) -> str:
    if isinstance(subpath, dict):
        types = get_types(subpath)
        subpath = "_".join(map(str.lower, types)) if types else "object"
    if isinstance(actor, Mapping):
        actor_uri = get_id(actor)
    elif isinstance(actor, str):
        actor_uri = actor
    else:
        raise ValueError("Invalid actor")
    return f"{actor_uri}/{subpath}/{uuid.uuid4()}"
