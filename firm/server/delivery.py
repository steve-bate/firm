import asyncio
import copy
import hashlib
import logging
import time
from typing import Awaitable, Callable, Iterable, Mapping, cast

from firm.core.auth.authorization import CoreAuthorizationService
from firm.core.auth.http_signature import HttpSignatureAuth
from firm.core.delivery.queue import (
    Delivery,
    DeliveryQueue,
    DeliveryState,
    MemoryDeliveryQueue,
)
from firm.core.interfaces import (
    FIRM_NS,
    JSON,
    APActor,
    AuthorizationService,
    DeliveryFailure,
    DeliveryReport,
    DeliveryService,
    HttpTransport,
    JSONObject,
    NoOpValidator,
    Principal,
    Tenant,
    Validator,
)
from firm.core.resolver import ResourceResolver
from firm.core.services.activitypub.exceptions import (
    InvalidRequestException,
    InvalidResourceTypeException,
    NotAuthorizedException,
    NotFoundException,
    ResourceOwnerException,
)
from firm.core.services.activitypub.service import post_to_inbox, resolve_box
from firm.core.services.activitypub.support import FirmContext
from firm.core.util import (
    AS2_CONTEXT,
    RECIPIENT_FIELDS,
    get_id,
    get_types,
    is_collection,
    is_public,
)
from firm.server.adapters import HttpxTransport
from firm.server.config import ServerConfig

log = logging.getLogger(__name__)


def _get_uris(items: list[JSONObject | str]) -> list[str]:
    uris: list[str] = []
    for item in items:
        if isinstance(item, str):
            uris.append(item)
        elif isinstance(item, dict) and "id" in item:
            uris.append(cast(str, item["id"]))
    return uris


def strip_firm_properties(value: JSON, predicate: Callable[[str], bool]) -> JSON:
    """A copy of ``value`` with every mapping key the predicate accepts removed.

    Pure: the caller's document and everything it embeds stay as they were, so a
    delivery cannot rewrite what the store holds.
    """
    if isinstance(value, Mapping):
        return {
            key: strip_firm_properties(item, predicate)
            for key, item in value.items()
            if not predicate(str(key))
        }
    if isinstance(value, list):
        return [strip_firm_properties(item, predicate) for item in value]
    return value


def _inbox_of(document: JSONObject, *, local: bool) -> str | None:
    """The inbox to post to, or None when the document has none.

    Every actor must have an ``inbox``; a shared inbox is optional. For a remote
    recipient the shared inbox collapses a followers list into one request, which is
    what it is for, so it is preferred when advertised. A local recipient is written
    into its own box: our shared inbox is a route, not a stored collection, so there
    would be nothing to append to.
    """
    inbox = document.get("inbox")
    if not isinstance(inbox, str):
        return None
    if local:
        return inbox
    endpoints = document.get("endpoints")
    if isinstance(endpoints, Mapping):
        shared = endpoints.get("sharedInbox")
        if isinstance(shared, str):
            return shared
    return inbox


class DeliveryError(Exception):
    """One inbox, or the activity itself, could not be delivered.

    ``status`` is the peer's HTTP answer when there was one, so the queue can tell a
    refusal (terminal) from a fault (worth another round).
    """

    def __init__(self, reason: str, attempts: int, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts
        self.status = status


class DeliveryRefused(DeliveryError):
    """The receiving instance refused the activity; a retry will not change its mind."""


def _is_retryable(status_code: int) -> bool:
    """A server fault or a throttle may clear; a refusal will not."""
    return status_code >= 500 or status_code in {408, 425, 429}


def _delivery_id(tenant: Tenant, activity_uri: str, endpoint: str) -> str:
    """A stable id for one activity owed to one endpoint, so enqueueing twice is harmless."""
    seed = "\n".join((str(tenant.prefix), activity_uri, endpoint))
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


class FirmDeliveryService(DeliveryService):
    def __init__(
        self,
        config: ServerConfig,
        transport: HttpTransport | None = None,
        validator: Validator | None = None,
        authorizer: AuthorizationService | None = None,
        queue: DeliveryQueue | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self._config = config
        self._delivery = config.delivery
        self._transport: HttpTransport = transport or HttpxTransport()
        self._validator: Validator = validator or NoOpValidator()
        self._authorizer: AuthorizationService = authorizer or CoreAuthorizationService()
        self._queue: DeliveryQueue = queue or MemoryDeliveryQueue(clock=clock)
        self._sleep = sleep
        self._clock = clock
        self._resolver: ResourceResolver | None = None
        self._tenants: Mapping[str, Tenant] = {}

    def _resolver_for(self, *tenants: Tenant) -> ResourceResolver:
        """The instance resolver, or one over the tenants this call can see."""
        if self._resolver is not None:
            return self._resolver
        return ResourceResolver({str(tenant.prefix): tenant for tenant in tenants})

    def set_resolver(self, resolver: ResourceResolver) -> None:
        """Attach the instance registry once the lifespan has built it."""
        self._resolver = resolver

    @property
    def resolver(self) -> ResourceResolver | None:
        return self._resolver

    async def _recipient_document(self, tenant: Tenant, uri: str) -> JSONObject | None:
        """The document for a recipient URI: a copy we hold, then its owner, then the network.

        A stored copy wins, even for a URI another server owns: the document we hold is
        the document we address, and re-fetching it would make delivery fail on an
        instance that cannot resolve the name for a reason the address book already knows.
        """
        document = await tenant.public_store.get(uri)
        if document is not None:
            return document
        if self._resolver is not None:
            owner = self._resolver.owner(uri)
            if owner is not None:
                return await owner.public_store.get(uri)
            return await self._resolver.fetch(uri)
        owner = self._owner_of(uri, tenant, self._tenant_map())
        return await owner.public_store.get(uri) if owner is not None else None

    async def _resolve_inboxes(self, tenant: Tenant, recipient_uris: Iterable[str]) -> set[str]:
        """Every inbox reachable from the activity's addressing, each once."""
        inboxes: set[str] = set()
        seen: set[str] = set()
        queue: list[tuple[str, int]] = [(uri, 0) for uri in recipient_uris]
        while queue:
            uri, depth = queue.pop(0)
            if uri in seen or is_public(uri):
                continue
            seen.add(uri)
            document = await self._recipient_document(tenant, uri)
            if document is None:
                log.info("Delivery target not resolvable: %s", uri)
                continue
            if is_collection(document):
                if depth >= self._delivery.max_expand_depth:
                    log.warning("Delivery expansion stopped at depth %d: %s", depth, uri)
                    continue
                items = document.get("items") or document.get("orderedItems") or []
                if isinstance(items, list):
                    stored = cast(list[JSONObject | str], items)
                    queue.extend((item, depth + 1) for item in _get_uris(stored))
                continue
            inbox_uri = _inbox_of(document, local=self._config.is_local(uri))
            if inbox_uri:
                inboxes.add(inbox_uri)
            else:
                log.warning("Recipient %s has no inbox", uri)
        return inboxes

    async def _post(
        self,
        inbox: str,
        /,
        message: JSONObject,
        auth: HttpSignatureAuth,
    ) -> None:
        reason = "no attempt was made"
        status: int | None = None
        for attempt in range(1, self._delivery.max_attempts + 1):
            try:
                response = await self._transport.post(
                    inbox,
                    json=message,
                    headers={"Content-Type": "application/activity+json"},
                    auth=auth,
                    timeout=self._delivery.timeout,
                )
            except Exception as exc:
                # A transport failure is a failed delivery, not a bug in this module.
                reason = f"{type(exc).__name__}: {exc}"
                status = None
            else:
                if 200 <= response.status_code < 300:
                    log.info(f"FirmDeliveryService POST {inbox} {response.status_code}")
                    return
                reason = f"HTTP {response.status_code}"
                status = response.status_code
                if not _is_retryable(status):
                    raise DeliveryError(reason, attempt, status=status)
            if attempt < self._delivery.max_attempts:
                await self._sleep(self._delivery.retry_backoff * 2 ** (attempt - 1))
        raise DeliveryError(reason, self._delivery.max_attempts, status=status)

    async def _serialize(self, tenant: Tenant, activity: JSONObject) -> JSONObject:
        message = copy.deepcopy(activity)
        message.pop("bto", None)
        message.pop("bcc", None)
        message = cast(
            JSONObject,
            strip_firm_properties(message, lambda key: key.startswith("firm:")),
        )
        # TODO Define message/property specific serialization
        # (collection paging, per-recipient visibility)
        if "@context" not in message:
            message["@context"] = AS2_CONTEXT
        object_uri = get_id(activity.get("object"))
        if (
            isinstance(activity.get("object"), str)
            and object_uri is not None
            and object_uri.startswith(tenant.prefix)
            and "Follow" not in get_types(activity)
        ):
            embedded = await tenant.public_store.get(object_uri)
            if embedded:
                message["object"] = copy.deepcopy(embedded)
        return message

    async def _signer(self, tenant: Tenant, actor_uri: str) -> HttpSignatureAuth:
        actor = await tenant.public_store.get(actor_uri)
        if not actor:
            raise DeliveryRefused(f"actor {actor_uri} is not in the tenant store", 1)
        key_uri = get_id(actor.get("publicKey", {}))
        if not key_uri:
            raise DeliveryRefused(f"actor {actor_uri} has no publicKey id", 1)
        credentials = await tenant.private_store.query_one(
            {
                "type": FIRM_NS.Credentials.value,
                "attributedTo": actor_uri,
            }
        )
        if not credentials:
            raise DeliveryRefused(f"no credentials for {actor_uri}", 1)
        private_key_pem = credentials.get(FIRM_NS.privateKey.value)
        if not isinstance(private_key_pem, str) or not private_key_pem:
            raise DeliveryRefused(f"no private key for {actor_uri}", 1)
        return HttpSignatureAuth(key_uri, private_key_pem)

    def _targets(self, activity: JSONObject) -> set[str]:
        """The addressees of an activity, minus the actor that published it (AP 7.1)."""
        actor_uri = get_id(activity.get("actor"))
        targets: set[str] = set()
        for prop in RECIPIENT_FIELDS:
            value = activity.get(prop)
            if isinstance(value, str):
                targets.add(value)
            elif isinstance(value, list):
                targets.update(uri for uri in value if isinstance(uri, str))
        targets.discard(actor_uri or "")
        return targets

    async def _deliver_locally(
        self,
        owner: Tenant,
        publisher: Tenant,
        document: JSONObject,
        inbox_uri: str,
    ) -> None:
        """Hand the activity to the owning tenant's inbox, exactly as a POST would."""
        actor_uri = get_id(document.get("actor"))
        actor = await publisher.public_store.get(actor_uri) if actor_uri else None
        if actor is None and actor_uri is not None:
            # A forwarded activity was authored elsewhere, so the actor is not in our
            # store: it comes from the resolver like any other remote document.
            actor = await self._recipient_document(publisher, actor_uri)
        if actor is None:
            raise DeliveryError(f"actor {actor_uri} is not stored", 1)
        context = FirmContext(
            self._resolver_for(owner, publisher),
            owner,
            Principal(cast(APActor, actor), publisher),
            self._authorizer,
        )
        try:
            _, box_owner = await resolve_box(owner.public_store, inbox_uri)
            await post_to_inbox(
                context,
                box_owner,
                document,
                box_uri=inbox_uri,
                validator=self._validator,
                delivery_service=self,
            )
        except (
            NotAuthorizedException,
            ResourceOwnerException,
            InvalidResourceTypeException,
        ) as exc:
            raise DeliveryRefused(str(exc), 1) from exc
        except (NotFoundException, InvalidRequestException, ValueError) as exc:
            raise DeliveryError(str(exc), 1) from exc

    async def deliver(
        self,
        tenant: Tenant,
        all_tenants: Mapping[str, Tenant],
        activity: JSONObject,
        *,
        local_only: bool = False,
    ) -> DeliveryReport:
        activity_uri = get_id(activity.get("id"))
        actor_uri = get_id(activity.get("actor"))
        if activity_uri is None:
            return DeliveryReport(failed=(DeliveryFailure(None, 0, "activity has no id"),))
        if actor_uri is None:
            return DeliveryReport(failed=(DeliveryFailure(None, 0, "activity has no actor"),))
        self._tenants = all_tenants
        inboxes = await self._resolve_inboxes(tenant, self._targets(activity))
        if local_only:
            # AP 7.1.2 forwarding: the origin already reached the addressees it could
            # see, so re-posting to them would be duplication, not forwarding.
            inboxes = {inbox for inbox in inboxes if self._config.is_local(inbox)}
        return await self._fan_out(tenant, all_tenants, activity, activity_uri, inboxes)

    async def run(self, delivery: Delivery, document: JSONObject | None = None) -> None:
        """Attempt one unit of work and record where it stands.

        ``document`` is the activity when the caller already holds it (the publisher
        does); a retry from the worker passes nothing and reads the store instead.
        """
        now = self._clock()
        try:
            await self._attempt(delivery, document)
        except DeliveryRefused as exc:
            # The receiver refused it; another round would be refused the same way.
            await self._queue.abandon(
                delivery.id, reason=exc.reason, attempts=delivery.attempts + 1
            )
        except DeliveryError as exc:
            if exc.status is not None and not _is_retryable(exc.status):
                # The peer answered and will answer the same way; the status is the reason.
                await self._queue.abandon(
                    delivery.id, reason=exc.reason, attempts=delivery.attempts + 1
                )
                return
            if now - delivery.created_at >= self._delivery.max_age:
                await self._queue.abandon(
                    delivery.id, reason=exc.reason, attempts=delivery.attempts + 1
                )
                return
            attempts = delivery.attempts + 1
            backoff = min(
                self._delivery.retry_backoff * 2 ** (attempts - 1),
                self._delivery.retry_backoff_max,
            )
            await self._queue.reschedule(
                delivery.id, next_attempt_at=now + backoff, reason=exc.reason
            )
        else:
            await self._queue.finish(delivery.id)

    def _tenant_map(self) -> Mapping[str, Tenant]:
        """The registry: the resolver's when one is attached, else the map `deliver` was handed."""
        if self._resolver is not None:
            return self._resolver.tenants
        return self._tenants

    async def _attempt(self, delivery: Delivery, document: JSONObject | None = None) -> None:
        tenants = self._tenant_map()
        tenant = tenants.get(delivery.tenant)
        if tenant is None:
            raise DeliveryError(f"unknown tenant {delivery.tenant}", 1)
        if document is None:
            document = await tenant.public_store.get(delivery.activity)
        if document is None:
            raise DeliveryError(f"activity {delivery.activity} is not stored", 1)
        if self._config.is_local(delivery.endpoint):
            owner = self._owner_of(delivery.endpoint, tenant, tenants)
            if owner is None:
                raise DeliveryError(f"unknown tenant for inbox {delivery.endpoint}", 1)
            await self._deliver_locally(owner, tenant, document, delivery.endpoint)
            return
        actor_uri = get_id(document.get("actor")) or ""
        auth = await self._signer(tenant, actor_uri)
        await self._post(delivery.endpoint, await self._serialize(tenant, document), auth)

    def _owner_of(
        self, inbox_uri: str, tenant: Tenant, all_tenants: Mapping[str, Tenant]
    ) -> Tenant | None:
        """The local tenant that owns an inbox URI, longest prefix first."""
        best: Tenant | None = None
        for prefix, candidate in all_tenants.items():
            if inbox_uri.startswith(prefix) and (best is None or len(prefix) > len(best.prefix)):
                best = candidate
        return best or (tenant if self._config.is_local(inbox_uri) else None)

    async def _fan_out(
        self,
        tenant: Tenant,
        all_tenants: Mapping[str, Tenant],
        activity: JSONObject,
        activity_uri: str,
        inboxes: set[str],
    ) -> DeliveryReport:
        """Record one unit per endpoint, then attempt them all, bounded."""
        self._tenants = all_tenants
        now = self._clock()
        deliveries = [
            Delivery(
                id=_delivery_id(tenant, activity_uri, inbox),
                tenant=str(tenant.prefix),
                activity=activity_uri,
                endpoint=inbox,
                created_at=now,
                next_attempt_at=now,
            )
            for inbox in sorted(inboxes)
        ]
        for delivery in deliveries:
            await self._queue.enqueue(delivery)
        semaphore = asyncio.Semaphore(self._delivery.concurrency)

        async def attempt_one(delivery: Delivery) -> None:
            async with semaphore:
                await self.run(delivery, activity)

        await asyncio.gather(*(attempt_one(delivery) for delivery in deliveries))
        delivered: list[str] = []
        failed: list[DeliveryFailure] = []
        for delivery in deliveries:
            row = await self._queue.get(delivery.id) or delivery
            if row.state is DeliveryState.DELIVERED:
                delivered.append(row.endpoint)
            else:
                failed.append(
                    DeliveryFailure(row.endpoint, row.attempts, row.last_reason or "not delivered")
                )
        return DeliveryReport(delivered=tuple(delivered), failed=tuple(failed))
