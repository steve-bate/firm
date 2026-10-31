import copy
import logging
from typing import Any, Mapping, cast
from urllib.parse import parse_qs

import jsonpath_rfc9535 as jsonpath  # type: ignore

from firm.core.interfaces import (
    APActor,
    AuthorizationService,
    DeliveryService,
    Identity,
    JSONObject,
    NoOpValidator,
    ResourceStore,
    Url,
    Validator,
)
from firm.core.services.activitypub.collections import (
    LAST,
    add_collection_item,
    build_page_document,
    dereference_collection_items,
    option_value,
    page_number,
    page_size,
    page_uri,
)
from firm.core.services.activitypub.exceptions import (
    InvalidRequestException,
    InvalidResourceException,
    InvalidResourceTypeException,
    NotAuthorizedException,
    NotFoundException,
    ResourceOwnerException,
)
from firm.core.services.activitypub.inbox import process_inbox
from firm.core.services.activitypub.outbox import process_outbox
from firm.core.services.activitypub.serialization import serialize
from firm.core.services.activitypub.support import FirmBoxContext, FirmContext
from firm.core.util import (
    RECIPIENT_FIELDS,
    dereference,
    get_id,
    get_list,
    has_value,
    is_accessible,
    is_activity,
    is_collection,
    is_public,
    set_collection_items,
    strip_query,
)

log = logging.getLogger(__name__)


def item_filter(expr: str) -> str:
    expr = (
        expr if expr.startswith("$") else f"$[?{expr}]" if not expr.startswith("[") else f"${expr}"
    )
    return expr


# How far into a nested object the "is this ours?" walk may look. Local copy of the
# idea `DeliveryConfig.max_expand_depth` expresses, because core cannot read the config.
_MAX_REFERENCE_DEPTH = 2

# AP 7.1.2 (3): the fields that can tie an activity to something this server owns.
_REFERENCE_FIELDS = ("inReplyTo", "object", "target", "tag")

# AP 7.1.2: the values that name the addressees of the *original* activity.
_ADDRESS_FIELDS = ("to", "cc", "audience")


def _target_uris(activity: JSONObject, fields: tuple[str, ...]) -> set[str]:
    """Every URI named by the given addressing fields."""
    uris: set[str] = set()
    for field in fields:
        value = activity.get(field)
        if isinstance(value, str):
            uris.add(value)
        elif isinstance(value, list):
            uris.update(uri for uri in value if isinstance(uri, str))
    return uris


def _references_something_of_ours(context: FirmContext, value: Any, depth: int = 0) -> bool:
    """Does this reference point at anything this server owns? Bounded, cycle-free."""
    if depth > _MAX_REFERENCE_DEPTH:
        return False
    if isinstance(value, str):
        return context.resolver.owner(value) is not None
    if isinstance(value, list):
        return any(_references_something_of_ours(context, item, depth) for item in value)
    if isinstance(value, Mapping):
        uri = get_id(value)
        if uri is not None and context.resolver.owner(str(uri)) is not None:
            return True
        return any(
            _references_something_of_ours(context, value[field], depth + 1)
            for field in _REFERENCE_FIELDS
            if field in value
        )
    return False


async def _is_our_collection(context: FirmContext, uri: str) -> bool:
    """Is this URI a collection stored on one of our tenants?"""
    owner = context.resolver.owner(uri)
    if owner is None:
        return False
    document = await owner.public_store.get(uri)
    return document is not None and is_collection(document)


async def _forward_to_local_addressees(
    context: FirmContext, activity: JSONObject, delivery_service: DeliveryService
) -> None:
    """AP 7.1.2: hand an inbox activity to our own addressees.

    The origin posted to every addressee it could resolve. When one of those is a
    collection we own, its members are invisible to the origin, so the reply would
    reach the box owner alone and everyone else would see a reply to nothing.
    """
    author = get_id(activity.get("actor"))
    if author is not None and context.resolver.owner(str(author)) is not None:
        # Our own activity: publishing already addressed our addressees, and forwarding
        # it again would re-enter this function on every hop.
        return
    if not any(
        _references_something_of_ours(context, activity.get(field)) for field in _REFERENCE_FIELDS
    ):
        return
    targets = {
        uri for uri in _target_uris(activity, _ADDRESS_FIELDS) if context.resolver.owner(uri)
    }
    if not any([await _is_our_collection(context, uri) for uri in targets]):
        return
    await delivery_service.deliver(
        context.tenant, context.resolver.tenants, activity, local_only=True
    )


def _get_query_param(params: Mapping[str, list[str]], key: str, default_value: str) -> str:
    values = params.get(key)
    if not values or len(values) == 0:
        return default_value
    return values[0]


async def _activity_query(principal, store, filter: jsonpath.JSONPathQuery | None = None):
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


def _addressed_to_box_owner(activity: JSONObject, box_owner: APActor) -> JSONObject:
    """An activity delivered to a personal inbox is addressed to its owner.

    The delivery is the claim: the author sent it here, so the owner is in the
    audience even when the document names nobody. The policy reads the document
    alone, so the claim has to be in it.
    """
    owner_uri = box_owner.get("id")
    if not isinstance(owner_uri, str):
        return activity
    obj = activity.get("object")
    if is_public(activity) or (isinstance(obj, dict) and is_public(obj)):
        return activity
    if any(owner_uri in get_list(activity, field) for field in RECIPIENT_FIELDS):
        return activity
    addressed = copy.deepcopy(activity)
    addressed["to"] = get_list(activity, "to") + [owner_uri]
    return addressed


async def resolve_box(store: ResourceStore, box_uri: str) -> tuple[JSONObject, APActor]:
    """The box document and the actor that owns it, or the reason there is none."""
    target = await dereference(store, box_uri)
    if not target:
        raise NotFoundException(box_uri)
    if not has_value(target, "type", "OrderedCollection"):
        raise InvalidResourceTypeException(target.get("type"), "OrderedCollection")
    box_owner_uri = target.get("attributedTo")
    if not isinstance(box_owner_uri, str):
        raise ResourceOwnerException("No owner for box")
    box_owner = await dereference(store, box_owner_uri)
    if not box_owner:
        raise ResourceOwnerException("Unknown box owner")
    return target, cast(APActor, box_owner)


async def post_to_inbox(
    context: FirmContext,
    box_owner: APActor,
    resource: JSONObject,
    *,
    box_uri: str,
    validator: Validator,
    delivery_service: DeliveryService,
) -> None:
    """An activity arrives at an inbox on this instance: authorize, keep, record, apply.

    This is what an inbox POST does once the box is known, with no socket involved.
    """
    tenant = context.tenant
    principal = context.principal
    store = tenant.public_store
    decision = await context.authorizer.is_post_authorized(tenant, principal, "inbox", box_uri)
    if not decision.authorized:
        raise NotAuthorizedException(decision.reason)
    validator.validate(resource)
    decision = await context.authorizer.is_activity_authorized(tenant, principal, resource)
    if not decision.authorized:
        raise NotAuthorizedException(decision.reason)
    # The delivery is what makes the owner a recipient; later steps (and the copy
    # kept in the box) see the same document.
    resource = _addressed_to_box_owner(resource, box_owner)
    log.info(f"Inbox: box={box_uri}, activity_type={resource.get('type')}")
    activity_id = get_id(resource)
    if activity_id is None:
        raise InvalidRequestException("Activity must have an id")
    # AP 7.1.2 asks whether this is the first time we have seen the activity: the
    # owner's inbox is our own record of what we accepted, and only the store knows
    # what it holds.
    inbox_uri = get_id(box_owner.get("inbox"))
    first_time = not (inbox_uri and await store.contains_item(inbox_uri, str(activity_id)))
    await context.keep(resource)
    await add_collection_item(store, box_owner["inbox"], str(activity_id))
    box_context = FirmBoxContext(
        context.resolver,
        tenant,
        principal,
        context.authorizer,
        box_owner=box_owner,
        box_uri=box_uri,
        delivery_service=delivery_service,
    )
    await process_inbox(box_context, resource)
    if first_time:
        await _forward_to_local_addressees(context, resource, delivery_service)


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
        context: FirmContext,
        resource_uri: Url,
        options: Mapping[str, Any] | None,
    ) -> JSONObject:
        """Handle requests to the shared inbox."""
        # if request.auth is None:
        #     raise NotAuthorizedException("Shared inbox requires authentication")
        tenant = context.tenant
        principal = context.principal
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
                for other in context.tenants.values():
                    other_store = other.public_store
                    all_public_activities.extend(
                        cast(list, await _activity_query(principal, other_store, filter))
                    )
            else:
                all_public_activities.extend(
                    cast(list, await _activity_query(principal, store, filter))
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

            return await serialize(context, page)

    async def process_get(
        self,
        context: FirmContext,
        resource_uri: Url,
        options: Mapping[str, Any] | None = None,
    ) -> JSONObject:
        tenant = context.tenant
        principal = context.principal
        # The query string belongs to the request, not to the resource being asked for.
        target = strip_query(str(resource_uri))
        # Return the tenant resource
        # FastAPI starlette always adds a trailing slash
        if tenant.prefix + "/" == target:
            doc = await tenant.public_store.get(str(tenant.prefix))
            if not doc:
                raise NotFoundException(resource_uri)
            if tenant.endpoints:
                doc["endpoints"] = tenant.endpoints
            return doc
        # Return a tenant's shared inbox resources
        if tenant.shared_inbox_uri and target.startswith(tenant.shared_inbox_uri):
            return await self._get_shared_inbox(context, resource_uri, options)
        # Return the specified resource
        store = tenant.public_store
        if resource := await dereference(store, target):
            decision = await self._authorizer.is_get_authorized(tenant, principal, resource)
            if decision.authorized:
                # A filtered collection is served whole: a filter needs every item.
                if is_collection(resource) and options and "filter" in options:
                    filter = jsonpath.compile(item_filter(options["filter"]))
                    items = await dereference_collection_items(context, resource)
                    filtered_nodes = filter.find(items)
                    set_collection_items(resource, [node.value for node in filtered_nodes])
                    return await serialize(context, resource)
                if is_collection(resource):
                    return await self._collection_response(
                        context, resource, str(resource_uri), options
                    )
                return await serialize(context, resource)
            else:
                raise NotAuthorizedException(decision.reason or "Not authorized")
        else:
            raise NotFoundException(resource_uri)

    async def _collection_response(
        self,
        context: FirmContext,
        collection: JSONObject,
        request_uri: str,
        options: Mapping[str, Any] | None,
    ) -> JSONObject:
        """A collection as the wire wants it: inline when small, a header or a page otherwise.

        A collection of at most one page's worth of items is served exactly as before, so no
        client that worked yesterday sees a different shape today. Anything larger needs a
        page URL to be read at all, which is what makes a bare GET cheap.
        """
        store = context.tenant.public_store
        collection_uri = get_id(collection) or strip_query(request_uri)
        try:
            limit = page_size(options)
        except ValueError as e:
            raise InvalidRequestException(str(e)) from None
        number = page_number(options)
        cursor = option_value(options, "cursor")
        total = await store.count_items(collection_uri)
        if number is None and cursor is None:
            if total <= limit:
                return await serialize(context, collection)
            served = await serialize(
                context, self._collection_header(collection, collection_uri, total, limit)
            )
            # The placeholder array kept serialization out of the item list; the header
            # carries links instead of items.
            served.pop("items", None)
            served.pop("orderedItems", None)
            return served
        if cursor is not None:
            page = await store.page_items(collection_uri, cursor, limit=limit)
        else:
            start = self._page_start(number, total, limit)
            page = await store.page_items(collection_uri, start=start, limit=limit)
        return await serialize(
            context,
            build_page_document(collection, page, request_uri=request_uri, limit=limit),
        )

    @staticmethod
    def _page_start(number: int | None, total: int, limit: int) -> int:
        """The newest-first index a page number starts at."""
        if number is None or number == 1:
            return 0
        if number == LAST:
            return max(total - limit, 0)
        return max((number - 1) * limit, 0)

    @staticmethod
    def _collection_header(
        collection: JSONObject, collection_uri: str, total: int, limit: int
    ) -> JSONObject:
        """The collection without its items, pointing at its first and last pages."""
        header: JSONObject = {
            key: value for key, value in collection.items() if key not in ("items", "orderedItems")
        }
        header["totalItems"] = total
        header["first"] = page_uri(collection_uri, page="true", limit=limit)
        header["last"] = page_uri(collection_uri, page="last", limit=limit)
        # An empty array keeps serialization from walking the collection it was told to
        # leave out; the key is removed from the served document below.
        header["items"] = []
        return header

    async def process_post(
        self,
        context: FirmContext,
        target_uri: Url,
        resource: JSONObject,
    ) -> str | None:
        tenant = context.tenant
        principal = context.principal
        # As in process_get: the query string is the request's, not the resource's.
        target_uri_str = strip_query(str(target_uri))
        if tenant.shared_inbox_uri and target_uri_str.startswith(tenant.shared_inbox_uri):
            raise NotImplementedError("Shared inbox does not support POST requests yet")
        store = tenant.public_store
        # All POST requests must be authenticated
        if principal is None:
            raise NotAuthorizedException("Not authenticated")
        target = await dereference(store, target_uri_str)
        if not target:
            raise NotFoundException(target_uri)
        # Boxes must be collections
        if not has_value(target, "type", "OrderedCollection"):
            raise InvalidResourceTypeException(target.get("type"), "OrderedCollection")
        # Found a box, now find the box owner
        box_owner_uri = target.get("attributedTo")
        if box_owner_uri is None or not isinstance(box_owner_uri, str):
            raise ResourceOwnerException("No owner for box")
        box_owner = await dereference(store, box_owner_uri)
        if not box_owner:
            raise ResourceOwnerException("Unknown box owner")

        # Determine the type of box and dispatch accordingly
        if target_uri_str == box_owner.get("inbox"):
            await post_to_inbox(
                context,
                cast(APActor, box_owner),
                resource,
                box_uri=target_uri_str,
                validator=self._validator,
                delivery_service=self._delivery_service,
            )
            return None
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
            box_context = FirmBoxContext(
                context.resolver,
                tenant,
                principal,
                context.authorizer,
                box_owner=box_owner,
                box_uri=target_uri_str,
                delivery_service=self._delivery_service,
            )
            return await process_outbox(box_context, resource)
        else:
            raise InvalidResourceException(target.get("type"), "Unsupported box type")
