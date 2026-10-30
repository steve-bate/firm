import logging
from typing import Callable, Iterable, Mapping, cast

import httpx

from firm.core.auth.http_signature import HttpSignatureAuth
from firm.core.interfaces import (
    FIRM_NS,
    DeliveryFailure,
    DeliveryReport,
    DeliveryService,
    JSONObject,
    ResourceStore,
    Tenant,
)
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


class FirmDeliveryService(DeliveryService):
    _RECIPIENT_PROPS = ["to", "cc", "bto", "bcc"]

    def __init__(self, config: ServerConfig):
        self._config = config

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
                    store = target_tenant.public_store
                    inbox = await store.get(inbox_uri)
                    if not inbox:
                        raise DeliveryError(f"inbox not found: {inbox_uri}", 1)
                    # inbox storage repr uses items
                    items = cast(list[JSONObject | str], inbox.get("items", []))
                    items.insert(0, activity_uri)
                    inbox["items"] = items
                    inbox["totalItems"] = len(items)
                    await store.put(inbox)
                else:
                    if message is None:
                        message = await self._serialize(tenant, activity)
                    await self._post(inbox_uri, message=message, auth=auth)
            except DeliveryError as exc:
                failed.append(DeliveryFailure(inbox_uri, exc.attempts, exc.reason))
            else:
                delivered.append(inbox_uri)
        return DeliveryReport(delivered=tuple(delivered), failed=tuple(failed))
