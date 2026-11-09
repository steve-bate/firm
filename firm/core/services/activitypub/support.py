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
from firm.core.services.activitypub.exceptions import (
    InvalidResourceException,
    NotAuthorizedException,
    NotFoundException,
)
from firm.core.services.activitypub.publish import outbox_send
from firm.core.util import get_id, get_types, is_local_uri, is_urn

log = logging.getLogger(__name__)

_DROP = object()
"""Stands for "this value must not be kept", so the CBD walk can remove it."""


def _attributed_to(document: JSONObject, uri: str) -> bool:
    """Whether ``document`` names ``uri`` as an actor it is attributed to.

    ``attributedTo`` may be a URI, an embedded node, or a list of either, so the
    comparison goes through ``get_id`` rather than the plain ``has_value``.
    """
    value = document.get("attributedTo")
    candidates = value if isinstance(value, list) else [value]
    return any(get_id(candidate) == uri for candidate in candidates if candidate)


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

    @property
    def _caller_uri(self) -> str | None:
        """The request's principal as an id, when the request has one."""
        return self.principal.uri if self.principal is not None else None

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

    async def keep(self, document: JSONObject, *, actor_uri: str | None = None) -> str | None:
        """Keep a document, and reference everything it embeds.

        Returns the id the document was kept under, or ``None`` when there is nothing to
        keep. The store holds concise bounded descriptions: the document's own properties,
        never a copy of a document it embeds. So an embedded node with an address of its
        own stays a reference in this document, and is never written over the document
        that address already names - a node carrying ``type`` and ``id`` says what an
        existing document *is*, it does not describe it. A node without an address is part
        of this document and stays inline - and so is a node addressed by a fragment
        (``…/actor#key``): a fragment names a part of the document that carries it, and no
        request can fetch it apart, so splitting it out would store something nothing can
        address.

        Replacing a document that is already stored is a different operation, with its own
        authority check: :meth:`replace`, used by ``Update``, ``Patch`` and ``Delete``.

        The caller's document is not modified: what the wire said is the caller's, what
        the store holds is this method's.

        Where a document belongs is decided by who owns its id - this tenant's own store,
        or the remote cache for a document another server authored. A document owned by
        another tenant of this instance is not stored here at all; the reference returned
        still names it, because that tenant's store holds it.

        ``actor_uri`` is the actor the document is stored as: the party whose namespace it
        may be brought into existence in. It defaults to the request's principal. A reply
        the server publishes passes its sender instead, because the principal that caused
        it - the remote actor who followed - may create nothing here.

        TODO: a document kept from an embedded node loses the parent's ``@context``, and
        its own may be absent. That is harmless today, because these documents are served
        and read as plain JSON, not as JSON-LD. An RDF store would need each kept document
        to carry the context it was expanded under, or the split would change its meaning.
        """
        actor = actor_uri if actor_uri is not None else self._caller_uri
        return await self._keep(copy.deepcopy(document), set(), actor)

    async def replace(self, document: JSONObject) -> str | None:
        """Write ``document`` over the one stored at its id — the ``Update`` path.

        Only for a document this instance already holds, and only for a principal that
        owns it: the stored copy decides who may replace it, never the incoming payload,
        which could otherwise claim any ``attributedTo`` it likes.
        """
        uri = get_id(document) or ""
        if not uri:
            raise InvalidResourceException("Document to replace has no ID")
        if not is_local_uri(self.tenant, uri):
            raise NotAuthorizedException(f"{uri} is not this instance's to replace")
        stored = await self.tenant.public_store.get(uri)
        if stored is None:
            raise NotFoundException(uri)
        if not await self._owns(stored, uri):
            raise NotAuthorizedException(f"The posting actor does not own {uri}")
        await self._split_embedded(document, set(), self._caller_uri)
        await self.tenant.public_store.put(document)
        return uri

    async def replace_cached(self, document: JSONObject) -> str | None:
        """Replace the copy this server caches of a document another server owns.

        The caller has authorized it. For an inbox ``Update`` that authority is AP 7.3:
        the object's origin, checked against the signer and, for an actor document,
        against the actor itself — which is why that check lives in the handler and this
        one only writes.
        """
        uri = get_id(document) or ""
        if not uri:
            raise InvalidResourceException("Document to replace has no ID")
        if is_local_uri(self.tenant, uri):
            raise NotAuthorizedException(
                f"{uri} is this instance's own: replace, not replace_cached"
            )
        await self._split_embedded(document, set(), self._caller_uri)
        await self.resolver.remember(document)
        return uri

    async def _owns(self, document: JSONObject, uri: str) -> bool:
        """Whether this request's principal may write the document stored at ``uri``.

        Ownership is the stored document's, in three steps: your own actor, a document
        attributed to you, or a document attributed to an actor that is itself yours —
        a video is attributed to its channel, and the channel to the account.
        """
        principal = self.principal
        if principal is None:
            return False
        if uri == principal.uri:
            return True
        value = document.get("attributedTo")
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            candidate_uri = get_id(candidate)
            if not candidate_uri:
                continue
            if candidate_uri == principal.uri:
                return True
            if is_local_uri(self.tenant, candidate_uri):
                owner = await self.tenant.public_store.get(candidate_uri)
                if owner is not None and _attributed_to(owner, principal.uri):
                    return True
        return False

    def _may_create(self, uri: str, actor_uri: str | None) -> bool:
        """Whether ``actor_uri`` may bring a document at ``uri`` into existence.

        Creating is not replacing: the only documents an actor may create are ones it will
        own - its own actor, or a URI under it. A node naming anything else is kept as a
        reference, so a client cannot pre-create documents at addresses another actor will
        later use. No actor at all (the server publishing its own activities) is trusted
        with the tenant's namespace, as before.
        """
        if actor_uri is None:
            return True
        return uri == actor_uri or uri.startswith(f"{actor_uri.rstrip('/')}/")

    async def _keep(
        self, document: JSONObject, seen: set[str], actor_uri: str | None
    ) -> str | None:
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
        if is_local_uri(self.tenant, uri):
            # A document this instance already holds is authoritative for its address.
            # What arrives embedded beside that address is a fragment about it, and a
            # fragment must never be written over the document itself: an actor would
            # lose its inbox, outbox and followers to ``{"type": …, "id": …}``. A real
            # replacement is an ``Update``, and goes through ``replace``.
            if await self.tenant.public_store.is_stored(uri):
                return uri
            if not self._may_create(uri, actor_uri):
                log.warning("Not creating %s: the storing actor may not own it", uri)
                return uri
            await self._split_embedded(document, seen, actor_uri)
            await self.tenant.public_store.put(document)
            return uri
        if self.resolver.owner(uri) is not None:
            log.warning("Not keeping a document owned by another tenant: %s", uri)
            return uri
        # A document another server owns, already cached: the cached copy is what this
        # server serves, and a fragment must not replace it either.
        if await self.resolver.peek(uri) is not None:
            return uri
        await self._split_embedded(document, seen, actor_uri)
        await self.resolver.remember(document)
        return uri

    async def _split_embedded(
        self, node: JSONObject, seen: set[str], actor_uri: str | None
    ) -> None:
        """Replace every document embedded in ``node`` with a reference to it."""
        for key, value in list(node.items()):
            replacement = await self._split_value(value, seen, actor_uri)
            if replacement is _DROP:
                node.pop(key, None)
            elif replacement is not value:
                node[key] = replacement

    async def _split_value(self, value: Any, seen: set[str], actor_uri: str | None) -> Any:
        """One property value: kept inline, or replaced by the reference it already is."""
        if isinstance(value, list):
            kept = [await self._split_value(item, seen, actor_uri) for item in value]
            return [item for item in kept if item is not _DROP]
        if not isinstance(value, Mapping):
            return value
        embedded: JSONObject = value if isinstance(value, dict) else dict(value)
        uri = get_id(embedded)
        if uri is None or urldefrag(uri)[1]:
            await self._split_embedded(embedded, seen, actor_uri)
            return embedded
        # An embedded node that carries an address is a reference to a document this one
        # does not own. `_keep` decides what may happen to that address: an existing
        # document is never written over, a missing one is created only for an actor
        # entitled to it, and a remote one is cached only when nothing is cached yet.
        reference = await self._keep(embedded, seen, actor_uri)
        return reference if reference is not None else uri


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
        await outbox_send(self, activity)


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
