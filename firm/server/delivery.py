import logging
from typing import Callable, Iterable, Mapping, cast

import httpx

from firm.core.auth.authorization import CoreAuthorizationService
from firm.core.auth.http_signature import HttpSignatureAuth
from firm.core.interfaces import (
    FIRM_NS,
    APActor,
    AuthorizationService,
    DeliveryFailure,
    DeliveryReport,
    DeliveryService,
    JSONObject,
    NoOpValidator,
    Principal,
    ResourceStore,
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
    get_id,
    get_prefix_uri,
    get_types,
    is_collection,
    is_public,
)
from firm.server.adapters import HttpxAuthAdapter
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


class DeliveryError(Exception):
    """One inbox, or the activity itself, could not be delivered."""

    def __init__(self, reason: str, attempts: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


class DeliveryRefused(DeliveryError):
    """The receiving instance refused the activity; a retry will not change its mind."""


class FirmDeliveryService(DeliveryService):
    _RECIPIENT_PROPS = ["to", "cc", "bto", "bcc"]

    def __init__(
        self,
        config: ServerConfig,
        validator: Validator | None = None,
        authorizer: AuthorizationService | None = None,
    ):
        self._config = config
        self._validator: Validator = validator or NoOpValidator()
        self._authorizer: AuthorizationService = authorizer or CoreAuthorizationService()
        self._resolver: ResourceResolver | None = None

    def _resolver_for(self, *tenants: Tenant) -> ResourceResolver:
        """The instance resolver, or one over the tenants this call can see."""
        if self._resolver is not None:
            return self._resolver
        return ResourceResolver({str(tenant.prefix): tenant for tenant in tenants})

    async def _resolve_inboxes(
        self,
        store: ResourceStore,
        recipient_uris: Iterable[str],
    ) -> set[str]:
        inboxes = set()
        for uri in recipient_uris:
            if is_public(uri):
                continue
            obj = await store.get(uri)
            if not obj:
                continue
            if is_collection(obj):  # TODO Expand server-local collections for inboxes
                # ... and self._store.is_local(uri):
                if items := cast(
                    list[JSONObject | str], obj.get("items") or obj.get("orderedItems")
                ):
                    for item in await self._resolve_inboxes(store, _get_uris(items)):
                        inboxes.add(item)
            else:
                if inbox_uri := obj.get("sharedInbox") or obj.get("inbox"):
                    inboxes.add(cast(str, inbox_uri))
        return inboxes

    def _remove_keys(self, obj: JSONObject, predicate: Callable[[str], bool]) -> None:
        for key, value in obj.items():
            if key.startswith("firm:"):
                obj.pop(key)
            else:
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            self._remove_keys(item, predicate)
                else:
                    if isinstance(value, dict):
                        self._remove_keys(value, predicate)

    async def _post(
        self,
        inbox: str,
        /,
        message: JSONObject,
        auth: HttpSignatureAuth,
    ) -> None:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    inbox,
                    json=message,
                    headers={"Content-Type": "application/activity+json"},
                    auth=HttpxAuthAdapter(auth),
                )
            except Exception as e:
                log.error(f"Error posting to {inbox}: {e}")
                return
            log.info(
                f"FirmDeliveryService POST {inbox} " f"{response.status_code} text={response.text}"
            )

    async def _serialize(self, tenant: Tenant, activity: JSONObject) -> JSONObject:
        message = cast(dict, activity).copy()
        message.pop("bto", None)
        message.pop("bcc", None)
        self._remove_keys(message, lambda k: k.startswith("firm:"))
        # TODO Define message/property specific serialization
        # (selected object embedding, collection paging, etc.)
        if isinstance(activity.get("object"), str) and "Follow" not in get_types(activity):
            uri = get_id(activity["object"])
            if uri:
                obj = await tenant.public_store.get(uri)
                if obj:
                    message["object"] = obj
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

    def _recipients(self, activity: JSONObject) -> set[str]:
        recipients: set[str] = set()
        for prop in self._RECIPIENT_PROPS:
            value = activity.get(prop)
            if isinstance(value, str):
                recipients.add(value)
            elif isinstance(value, list):
                recipients.update(uri for uri in value if isinstance(uri, str))
        return recipients

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
        inboxes = await self._resolve_inboxes(tenant.public_store, self._recipients(activity))
        delivered: list[str] = []
        failed: list[DeliveryFailure] = []
        message = None
        for inbox_uri in sorted(inboxes):
            try:
                if self._config.is_local(inbox_uri):
                    inbox_prefix = get_prefix_uri(inbox_uri)
                    target_tenant = (
                        tenant if inbox_prefix == tenant.prefix else all_tenants.get(inbox_prefix)
                    )
                    if not target_tenant:
                        raise DeliveryError(f"unknown tenant for inbox {inbox_uri}", 1)
                    await self._deliver_locally(target_tenant, tenant, activity, inbox_uri)
                else:
                    if message is None:
                        message = await self._serialize(tenant, activity)
                    await self._post(inbox_uri, message=message, auth=auth)
            except DeliveryError as exc:
                failed.append(DeliveryFailure(inbox_uri, exc.attempts, exc.reason))
            else:
                delivered.append(inbox_uri)
        return DeliveryReport(delivered=tuple(delivered), failed=tuple(failed))
