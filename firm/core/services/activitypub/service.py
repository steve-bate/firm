import logging
from typing import Any, Mapping, cast
from urllib.parse import parse_qs

import jsonpath_rfc9535 as jsonpath  # type: ignore

from firm.core.interfaces import (
    AuthorizationService,
    DeliveryService,
    Identity,
    JSONObject,
    NoOpValidator,
    ResourceStore,
    Tenant,
    Url,
    Validator,
)
from firm.core.services.activitypub.collections import (
    add_collection_item,
    dereference_collection_items,
)
from firm.core.services.activitypub.exceptions import (
    InvalidResourceException,
    InvalidResourceTypeException,
    NotAuthorizedException,
    NotFoundException,
    ResourceOwnerException,
)
from firm.core.services.activitypub.inbox import process_inbox
from firm.core.services.activitypub.outbox import outbox_send, process_outbox
from firm.core.services.activitypub.support import FirmBoxContext
from firm.core.util import (
    get_collection_items_key,
    has_value,
    is_accessible,
    is_activity,
    is_collection,
    is_type,
    resource_id,
    set_collection_items,
)

log = logging.getLogger(__name__)


def item_filter(expr: str) -> str:
    expr = (
        expr if expr.startswith("$") else f"$[?{expr}]" if not expr.startswith("[") else f"${expr}"
    )
    return expr


async def _dereference(store: ResourceStore, url: Url | str):
    if isinstance(url, Url):
        url = str(url)
    return await store.get(url)


async def _safe_dereference(store: ResourceStore, url: Url | str):
    if resource := await _dereference(store, url):
        return resource
    raise Exception(f"Resource not found: {url}")


async def _safe_dereference_or_uri(store: ResourceStore, url: Url | str):
    try:
        return await _safe_dereference(store, url)
    except Exception:
        return str(url)


async def _serialize(tenant: Tenant, resource: JSONObject) -> JSONObject:
    """Embed specific resources to match typical AP expectations."""

    store = tenant.public_store

    if not isinstance(resource, dict):
        raise Exception("Can only serialize JSON objects")

    elif is_collection(resource):
        resource = cast(
            JSONObject, await dereference_collection_items(store, resource, inplace=True)
        )
        items_key = get_collection_items_key(resource)
        if items_key != "items":
            # This is a bit hacky, but it allows us to store collections in a consistent way
            resource[items_key] = resource.pop("items")

    elif is_type(resource, "Create") or is_type(resource, "Update"):
        if isinstance(resource.get("object"), str):
            obj = await _safe_dereference(store, cast(str, resource["object"]))
            resource["object"] = obj
            for prop in ["likes", "shares"]:
                if prop in obj:
                    collection = await _safe_dereference(store, cast(str, obj[prop]))
                    collection.pop("items")
                    collection.pop("attributedTo")
                    obj[prop] = collection

    elif "endpoints" in resource:
        # TODO This endpoint handling is a bit hacky
        resource["endpoints"] |= tenant.endpoints

    return _remove_empty_arrays(resource)


def _get_query_param(params: Mapping[str, list[str]], key: str, default_value: str) -> str:
    values = params.get(key)
    if not values or len(values) == 0:
        return default_value
    return values[0]


def _remove_empty_arrays(resource: JSONObject) -> JSONObject:
    for key in list(resource.keys()):
        value = resource[key]
        if isinstance(value, list) and len(value) == 0:
            del resource[key]
        elif isinstance(value, dict):
            _remove_empty_arrays(value)
    return resource


async def _get_activities(principal, store, filter: jsonpath.JSONPathQuery | None = None):
    activities = [
        activity
        for activity in await store.query({})
        if is_activity(activity) and is_accessible(principal.uri if principal else None, activity)
    ]
    if filter:
        activities = [node.value for node in filter.find(activities)]
    return activities


def _assert_authorized_actor(principal: Identity | None, actor_uri):
    if principal is None or actor_uri != principal.uri:
        raise NotAuthorizedException()


class ActivityPubService:
    def __init__(
        self,
        authorizer: AuthorizationService,
        delivery_service: DeliveryService,
        validator: Validator = NoOpValidator(),
    ) -> None:
        self._authorizer = authorizer
        self._delivery_service = delivery_service
        self._validator = validator

    async def _get_shared_inbox(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        resource_uri: Url,
        options: Mapping[str, Any] | None,
    ) -> JSONObject:
        """Handle requests to the shared inbox."""
        # if request.auth is None:
        #     raise NotAuthorizedException("Shared inbox requires authentication")
        store = tenant.public_store
        default_page_Size = 20
        # TODO Implement pagination
        # This is brute force and limited for now, but we can optimize later
        # It's primarily here for Flowz demonstration purposes
        query_params = parse_qs(resource_uri.query)
        path_parts = resource_uri.path.split("/")
        federated = len(path_parts) == 3 and path_parts[-1] == "federated"
        offset_param = _get_query_param(query_params, "offset", "")
        limit = int(_get_query_param(query_params, "limit", str(default_page_Size)))
        # strip query and fragment
        box_id = f"{resource_uri.scheme}://{resource_uri.netloc}{resource_uri.path}"
        if offset_param == "":
            return {
                "@context": "https://www.w3.org/ns/activitystreams",
                "id": box_id,
                "type": "OrderedCollection",
                "first": f"{box_id}?offset=0",
            }
        else:
            filter = jsonpath.compile(
                item_filter(str(options.get("filter"))) if options and "filter" in options else None
            )
            offset = int(offset_param)
            all_public_activities: list[JSONObject] = []
            if federated:
                # This is obviously not scalable, but for demonstration purposes
                for tenant in tenants.values():
                    tenant_store = tenant.public_store
                    all_public_activities.extend(
                        cast(list, await _get_activities(principal, tenant_store, filter))
                    )
            else:
                all_public_activities.extend(
                    cast(list, await _get_activities(principal, store, filter))
                )

            items = all_public_activities[offset:limit]

            page: JSONObject = {
                "@context": "https://www.w3.org/ns/activitystreams",
                "id": f"{box_id}?offset={offset_param}",
                "type": "CollectionPage",
                "totalItems": len(items),
                "items": items,
            }

            if offset > 0:
                page["first"] = f"{box_id}?offset=0"
            if offset >= limit:
                page["prev"] = f"{box_id}?offset={max(0, offset - limit)}"
            if len(items) > 0 and (offset + limit < len(all_public_activities)):
                page["next"] = f"{box_id}?offset={int(offset) + limit}"

            return await _serialize(tenant, page)

    async def process_get(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        resource_uri: Url,
        options: Mapping[str, Any] | None = None,
    ) -> JSONObject:
        # FastAPI starlette always adds a trailing slash
        if tenant.prefix + "/" == str(resource_uri):
            doc = await tenant.public_store.get(str(tenant.prefix))
            if not doc:
                raise NotFoundException(resource_uri)
            if tenant.endpoints:
                doc["endpoints"] = tenant.endpoints
            return doc
        if tenant.shared_inbox_uri and str(resource_uri).startswith(tenant.shared_inbox_uri):
            return await self._get_shared_inbox(tenants, tenant, principal, resource_uri, options)
        store = tenant.public_store
        if resource := await _dereference(store, resource_uri):
            decision = await self._authorizer.is_get_authorized(tenant, principal, resource)
            if decision.authorized:
                if is_collection(resource) and options and "filter" in options:
                    filter = jsonpath.compile(item_filter(options["filter"]))
                    items = await dereference_collection_items(store, resource)
                    filtered_nodes = filter.find(items)
                    set_collection_items(resource, [node.value for node in filtered_nodes])
                return await _serialize(tenant, resource)
            else:
                raise NotAuthorizedException(decision.reason or "Not authorized")
        else:
            raise NotFoundException(resource_uri)

    async def process_post(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        target_uri: Url,
        resource: JSONObject,
    ) -> str | None:
        target_uri_str = str(target_uri)
        if tenant.shared_inbox_uri and str(target_uri_str).startswith(tenant.shared_inbox_uri):
            raise NotImplementedError("Shared inbox does not support POST requests yet")
        store = tenant.public_store
        # All POST requests must be authenticated
        if principal is None:
            raise NotAuthorizedException("Not authenticated")
        target = await _dereference(store, target_uri)
        if not target:
            raise NotFoundException(target_uri)
        # Boxes must be collections
        if not has_value(target, "type", "OrderedCollection"):
            raise InvalidResourceTypeException(target.get("type"), "OrderedCollection")
        # Found a box, now find the box owner
        box_owner_uri = target.get("attributedTo")
        if box_owner_uri is None or not isinstance(box_owner_uri, str):
            raise ResourceOwnerException("No owner for box")
        box_owner = await _dereference(store, box_owner_uri)
        if not box_owner:
            raise ResourceOwnerException("Unknown box owner")

        async def _send(activity: JSONObject) -> None:
            await outbox_send(
                tenant, tenants, activity, box_owner.get("outbox"), self._delivery_service
            )

        # Determine the type of box and dispatch accordingly
        if target_uri_str == box_owner.get("inbox"):
            decision = await self._authorizer.is_post_authorized(
                tenant, principal, "inbox", target_uri_str
            )
            if decision.authorized:
                store = tenant.public_store
                self._validator.validate(resource)
                decision = await self._authorizer.is_activity_authorized(
                    tenant, principal, resource
                )
                if not decision.authorized:
                    raise NotAuthorizedException(decision.reason)
                log.info(f"Inbox: box={target_uri}, activity_type={resource.get('type')}")
                await store.put(resource)
                await add_collection_item(store, box_owner["inbox"], resource_id(resource))
                context = FirmBoxContext(
                    _send,
                    tenants=tenants,
                    tenant=tenant,
                    principal=principal,
                    box_owner=box_owner,
                    box_uri=target_uri_str,
                )
                await process_inbox(context, resource)
                return None
            else:
                raise NotAuthorizedException(decision.reason)
        elif target_uri_str == box_owner.get("outbox"):
            box_decision = await self._authorizer.is_post_authorized(
                tenant, principal, "outbox", target_uri_str
            )
            if not box_decision.authorized:
                raise NotAuthorizedException(box_decision.reason)
            activity_decision = await self._authorizer.is_activity_authorized(
                tenant, principal, resource
            )
            if not activity_decision.authorized:
                raise NotAuthorizedException(activity_decision.reason)
            self._validator.validate(resource)
            context = FirmBoxContext(
                _send,
                tenants=tenants,
                tenant=tenant,
                principal=principal,
                box_owner=box_owner,
                box_uri=target_uri_str,
            )
            return await process_outbox(context, resource, self._delivery_service)
        else:
            raise InvalidResourceException(target.get("type"), "Unsupported box type")
