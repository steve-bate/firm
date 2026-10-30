import asyncio
import copy
import logging
from typing import Awaitable, Callable, Iterable, Mapping, cast

from firm.core.auth.authorization import CoreAuthorizationService
from firm.core.auth.http_signature import HttpSignatureAuth
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


class FirmDeliveryService(DeliveryService):
    def __init__(
        self,
        config: ServerConfig,
        transport: HttpTransport | None = None,
        validator: Validator | None = None,
        authorizer: AuthorizationService | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._config = config
        self._delivery = config.delivery
        self._transport: HttpTransport = transport or HttpxTransport()
        self._validator: Validator = validator or NoOpValidator()
        self._authorizer: AuthorizationService = authorizer or CoreAuthorizationService()
        self._sleep = sleep
        self._resolver: ResourceResolver | None = None

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
        """The document for a recipient URI: its owner's store, or the network."""
        if self._resolver is not None:
            owner = self._resolver.owner(uri)
            if owner is not None:
                return await owner.public_store.get(uri)
            return await self._resolver.fetch(uri)
        if uri.startswith(tenant.prefix):
            return await tenant.public_store.get(uri)
        return None

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
            raise DeliveryError(f"actor {actor_uri} is not in the tenant store", 0)
        key_uri = get_id(actor.get("publicKey", {}))
        if not key_uri:
            raise DeliveryError(f"actor {actor_uri} has no publicKey id", 0)
        credentials = await tenant.private_store.query_one(
            {
                "type": FIRM_NS.Credentials.value,
                "attributedTo": actor_uri,
            }
        )
        if not credentials:
            raise DeliveryError(f"no credentials for {actor_uri}", 0)
        private_key_pem = credentials.get(FIRM_NS.privateKey.value)
        if not isinstance(private_key_pem, str) or not private_key_pem:
            raise DeliveryError(f"no private key for {actor_uri}", 0)
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
    ) -> DeliveryReport:
        activity_uri = get_id(activity.get("id"))
        actor_uri = get_id(activity.get("actor"))
        if activity_uri is None:
            return DeliveryReport(failed=(DeliveryFailure(None, 0, "activity has no id"),))
        if actor_uri is None:
            return DeliveryReport(failed=(DeliveryFailure(None, 0, "activity has no actor"),))
        try:
            auth = await self._signer(tenant, actor_uri)
        except DeliveryError as exc:
            return DeliveryReport(failed=(DeliveryFailure(None, exc.attempts, exc.reason),))
        inboxes = await self._resolve_inboxes(tenant, self._targets(activity))
        return await self._fan_out(tenant, all_tenants, activity, activity_uri, inboxes, auth)

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
        auth: HttpSignatureAuth,
    ) -> DeliveryReport:
        """Send to every inbox, bounded, and report what happened to each."""
        message: JSONObject | None = None
        if any(not self._config.is_local(inbox) for inbox in inboxes):
            message = await self._serialize(tenant, activity)
        semaphore = asyncio.Semaphore(self._delivery.concurrency)
        serialized = message

        async def deliver_one(inbox: str) -> str:
            async with semaphore:
                if self._config.is_local(inbox):
                    owner = self._owner_of(inbox, tenant, all_tenants)
                    if owner is None:
                        raise DeliveryError(f"unknown tenant for inbox {inbox}", 1)
                    await self._deliver_locally(owner, tenant, activity, inbox)
                else:
                    if serialized is None:
                        raise DeliveryError(f"nothing to post to {inbox}", 1)
                    await self._post(inbox, serialized, auth)
                return inbox

        ordered = sorted(inboxes)
        results = await asyncio.gather(
            *(deliver_one(inbox) for inbox in ordered), return_exceptions=True
        )
        delivered: list[str] = []
        failed: list[DeliveryFailure] = []
        for inbox, result in zip(ordered, results):
            if isinstance(result, BaseException):
                if isinstance(result, DeliveryError):
                    failed.append(DeliveryFailure(inbox, result.attempts, result.reason))
                else:
                    raise result
            else:
                delivered.append(result)
        return DeliveryReport(delivered=tuple(delivered), failed=tuple(failed))
