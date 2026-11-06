import copy
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Mapping, overload
from urllib.parse import urldefrag

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
from firm.core.util import get_id, get_types, is_local_uri, is_urn, resource_id

log = logging.getLogger(__name__)

_DROP = object()
"""Stands for "this value must not be kept", so the CBD walk can remove it."""


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

    async def keep(self, document: JSONObject) -> str | None:
        """Keep a document that arrived from another server, and everything it embeds.

        Returns the id the document was kept under, or ``None`` when there is nothing to
        keep. The store holds concise bounded descriptions: the document's own properties,
        never a copy of a document it embeds. So every embedded document with an address of
        its own is kept as a document of its own first, and replaced here by a reference to
        it. A node without an address is part of this document and stays inline - and so is
        a node addressed by a fragment (``…/actor#key``): a fragment names a part of the
        document that carries it, and no request can fetch it apart, so splitting it out
        would store something nothing can address.

        The caller's document is not modified: what the wire said is the caller's, what
        the store holds is this method's.

        Where a document belongs is decided by who owns its id - this tenant's own store,
        or the remote cache for a document another server authored. A document owned by
        another tenant of this instance is not stored here at all; the reference returned
        still names it, because that tenant's store holds it.

        TODO: a document split out of its parent loses the parent's ``@context``, and its
        own may be absent. That is harmless today, because these documents are served and
        read as plain JSON, not as JSON-LD. An RDF store would need each kept document to
        carry the context it was expanded under, or the split would change its meaning.
        """
        return await self._keep(copy.deepcopy(document), set())

    async def _keep(self, document: JSONObject, seen: set[str]) -> str | None:
        """Store one document, after everything it embeds, and return the id it went under.

        ``seen`` is the set of ids this walk has already handled, so a document that
        refers back to one of its ancestors becomes a reference rather than a loop.
        """
        uri = get_id(document) or ""
        if not uri:
            log.debug("Not keeping a document without an id")
            return None
        if is_urn(uri):
            # A URN names a document of this tenant's own store: no peer can serve one, so
            # this is either a forged reference into our namespace or a document meant for
            # another server. Neither is something to hold.
            log.warning("Not keeping a URN-addressed document: %s", uri)
            return None
        if uri in seen:
            return uri
        seen.add(uri)
        await self._split_embedded(document, seen)
        if is_local_uri(self.tenant, uri):
            await self.tenant.public_store.put(document)
            return uri
        if self.resolver.owner(uri) is not None:
            log.warning("Not keeping a document owned by another tenant: %s", uri)
            return uri
        await self.resolver.remember(document)
        return uri

    async def _split_embedded(self, node: JSONObject, seen: set[str]) -> None:
        """Replace every document embedded in ``node`` with a reference to it."""
        for key, value in list(node.items()):
            replacement = await self._split_value(value, seen)
            if replacement is _DROP:
                node.pop(key, None)
            elif replacement is not value:
                node[key] = replacement

    async def _split_value(self, value: Any, seen: set[str]) -> Any:
        """One property value, kept, referenced, or dropped."""
        if isinstance(value, list):
            kept = [await self._split_value(item, seen) for item in value]
            return [item for item in kept if item is not _DROP]
        if not isinstance(value, Mapping):
            return value
        embedded: JSONObject = value if isinstance(value, dict) else dict(value)
        uri = get_id(embedded)
        if uri is None or urldefrag(uri)[1]:
            await self._split_embedded(embedded, seen)
            return embedded
        reference = await self._keep(embedded, seen)
        return reference if reference is not None else _DROP


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
        await outbox_send(self, activity, outbox_uri, self.delivery_service)


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
