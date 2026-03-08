import logging
from typing import Callable, Iterable, Mapping, cast

import httpx

from firm.core.auth.http_signature import HttpSignatureAuth
from firm.core.interfaces import (
    FIRM_NS,
    DeliveryService,
    JSONObject,
    ResourceStore,
    Tenant,
)
from firm.core.util import get_id, get_prefix_uri, get_types, is_collection, is_public
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

    async def deliver(
        self,
        tenant: Tenant,
        all_tenants: Mapping[str, Tenant],
        activity: JSONObject,
    ) -> None:
        # TODO Delivery - Handle failures and redelivery
        actor = await tenant.public_store.get(cast(str, activity["actor"]))
        if not actor:
            log.error("Actor not found for activity: %s", activity["actor"])
            return
        key_uri = get_id(actor.get("publicKey", {}))
        if not key_uri:
            log.error("No key for actor %s", actor["id"])
            return
        credentials = await tenant.private_store.query_one(
            {
                "type": FIRM_NS.Credentials.value,
                "attributedTo": actor["id"],
            }
        )
        if not credentials:
            log.error("No credentials found for actor %s", actor["id"])
            return
        private_key_pem = cast(str, credentials.get(FIRM_NS.privateKey.value))
        if not private_key_pem:
            log.error("No private key found for actor %s", actor["id"])
            return
        auth = HttpSignatureAuth(key_uri, private_key_pem)
        recipient_uris: set[str] = set()
        for prop in self._RECIPIENT_PROPS:
            if r := activity.get(prop):
                if isinstance(r, str):
                    recipient_uris.add(r)
                elif isinstance(r, list):
                    recipient_uris.update(r)
        inboxes = await self._resolve_inboxes(tenant.public_store, recipient_uris)
        message = None
        for inbox_uri in inboxes:
            if self._config.is_local(inbox_uri):
                inbox_prefix = get_prefix_uri(inbox_uri)
                target_tenant = (
                    tenant if inbox_prefix == tenant.prefix else all_tenants.get(inbox_prefix)
                )
                if not target_tenant:
                    log.error(f"Unknown tenant for inbox {inbox_uri}")
                    continue
                store = target_tenant.public_store
                inbox = await store.get(inbox_uri)
                if not inbox:
                    log.error(f"Inbox not found: {inbox_uri}")
                    continue
                items = cast(list[JSONObject | str], inbox.get("orderedItems", []))
                items.insert(0, cast(str, activity["id"]))
                inbox["orderedItems"] = items
                await store.put(inbox)
            else:
                if message is None:
                    message = await self._serialize(tenant, activity)
                await self._post(inbox_uri, message=message, auth=auth)
