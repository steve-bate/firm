import logging
import uuid
from datetime import datetime
from typing import Any, Mapping, cast
from urllib.parse import parse_qs

import jsonpath_rfc9535 as jsonpath  # type: ignore

from firm.core.interfaces import (
    FIRM_NS,
    URI,
    APActor,
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
from firm.core.services.exception import ServiceException
from firm.core.util import (
    ACTIVITIES_REQUIRING_OBJECT,
    ACTIVITIES_REQUIRING_TARGET,
    get_collection_items_key,
    get_list,
    get_types,
    has_value,
    is_accessible,
    is_activity,
    is_collection,
    is_type,
    log,
    resource_get,
    resource_id,
    set_collection_items,
)


class NotAuthorizedException(ServiceException):
    def __init__(self, reason: str | None = None):
        super().__init__(reason or "Not authorized")

    @property
    def reason(self):
        return self.args[0]


class NotFoundException(ServiceException):
    def __init__(self, url: Any):
        super().__init__(str(url))

    @property
    def url(self):
        return self.args[0]


class InvalidRequestException(ServiceException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class InvalidResourceTypeException(InvalidRequestException):
    def __init__(self, actual_type: Any, expected_type: Any):
        super().__init__(f"Invalid resource type: {actual_type}, expected: {expected_type}")

    @property
    def actual_type(self):
        return self.args[0]

    @property
    def expected_type(self):
        return self.args[1]


class ResourceOwnerException(InvalidRequestException): ...


class InvalidResourceException(InvalidRequestException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class ActivityPubTenant:
    def __init__(
        self,
        authorizer: AuthorizationService,
        delivery_service: DeliveryService,
        validator: Validator = NoOpValidator(),
    ):
        self._authorizer = authorizer
        self._delivery_service = delivery_service
        self._validator = validator

    async def _dereference(self, store: ResourceStore, url: Url | str):
        if isinstance(url, Url):
            url = str(url)
        return await store.get(url)

    async def _safe_dereference(self, store: ResourceStore, url: Url | str):
        if resource := await self._dereference(store, url):
            return resource
        raise Exception(f"Resource not found: {url}")

    async def _safe_dereference_or_uri(self, store: ResourceStore, url: Url | str):
        try:
            return await self._safe_dereference(store, url)
        except Exception:
            return str(url)

    async def _dereference_collection_items(
        self, store: ResourceStore, collection: JSONObject, inplace=False
    ) -> list[JSONObject] | JSONObject | None:
        items_key = get_collection_items_key(collection)
        items = collection.get(items_key, [])
        if isinstance(items, list):
            dereferenced_items: list[JSONObject] = []
            for item in items:
                if isinstance(item, str):
                    dereferenced_item = await self._dereference(store, item)
                    if dereferenced_item:
                        dereferenced_items.append(dereferenced_item)
                    else:
                        log.warning(f"Unable to dereference collection item: {item}")
                else:
                    dereferenced_items.append(item)
            if inplace:
                collection[items_key] = dereferenced_items
                return collection
            else:
                return dereferenced_items
        raise Exception("Collection items must be a list")

    @classmethod
    def _remove_empty_arrays(cls, resource: JSONObject) -> JSONObject:
        for key in list(resource.keys()):
            value = resource[key]
            if isinstance(value, list) and len(value) == 0:
                del resource[key]
            elif isinstance(value, dict):
                cls._remove_empty_arrays(value)
        return resource

    async def serialize(self, store: ResourceStore, resource: JSONObject) -> JSONObject:
        """Embed specific resources to match typical AP expectations."""
        if not isinstance(resource, dict):
            raise Exception("Can only serialize JSON objects")

        elif is_collection(resource):
            # FIXME This is messy
            resource = cast(
                JSONObject, await self._dereference_collection_items(store, resource, inplace=True)
            )

        # if is_collection(resource):
        #     # Keep it simple for now
        #     items_key = "items" if "items" in resource else "orderedItems"
        #     items = [
        #         await self.serialize(
        #             store,
        #             await self._safe_dereference_or_uri(store, i) if isinstance(i, str) else i,
        #         )
        #         for i in cast(list, resource.get(items_key, []))
        #     ]
        #     # Embed activity objects
        #     for item in items:
        #         if (
        #             isinstance(item, Mapping)
        #             and "actor" in item
        #             and isinstance(item.get("object"), str)
        #         ):
        #             item["object"] = await self._dereference(store, cast(str, item["object"]))
        #     # TODO Add more general support for empty array serialization
        #     if not items:
        #         if items_key in resource:
        #             del resource[items_key]
        #     else:
        #         resource[items_key] = items

        elif is_type(resource, "Create") or is_type(resource, "Update"):
            if isinstance(resource.get("object"), str):
                obj = await self._safe_dereference(store, cast(str, resource["object"]))
                resource["object"] = obj
                for prop in ["likes", "shares"]:
                    if prop in obj:
                        collection = await self._safe_dereference(store, cast(str, obj[prop]))
                        collection.pop("items")
                        collection.pop("attributedTo")
                        obj[prop] = collection

        return self._remove_empty_arrays(resource)

    @staticmethod
    def _get_query_param(params: Mapping[str, list[str]], key: str, default_value: str) -> str:
        values = params.get(key)
        if not values or len(values) == 0:
            return default_value
        return values[0]

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
        offset_param = self._get_query_param(query_params, "offset", "")
        limit = int(self._get_query_param(query_params, "limit", str(default_page_Size)))
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
            filter = (
                jsonpath.compile(options.get("filter")) if options and "filter" in options else None
            )
            offset = int(offset_param)
            all_public_activities: list[JSONObject] = []
            if federated:
                # This is obviously not scalable, but for demonstration purposes
                for tenant in tenants.values():
                    tenant_store = tenant.public_store
                    all_public_activities.extend(
                        cast(list, await self._get_activities(principal, tenant_store, filter))
                    )
            else:
                all_public_activities.extend(
                    cast(list, await self._get_activities(principal, store, filter))
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

            return await self.serialize(store, page)

    async def _get_activities(self, principal, store, filter: jsonpath.JSONPathQuery | None = None):
        activities = [
            activity
            for activity in await store.query({})
            if is_activity(activity)
            and is_accessible(principal.uri if principal else None, activity)
        ]
        if filter:
            activities = [node.value for node in filter.find(activities)]
        return activities

    async def _process_get(
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
        if resource := await self._dereference(store, resource_uri):
            decision = await self._authorizer.is_get_authorized(tenant, principal, resource)
            if decision.authorized:
                if is_collection(resource) and options and "filter" in options:
                    filter = jsonpath.compile(options["filter"])
                    items = await self._dereference_collection_items(store, resource)
                    filtered_nodes = filter.find(items)
                    set_collection_items(resource, [node.value for node in filtered_nodes])
                return await self.serialize(store, resource)
            else:
                raise NotAuthorizedException(decision.reason or "Not authorized")
        else:
            raise NotFoundException(resource_uri)

    async def _process_post(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        target_uri: Url,
        resource: JSONObject,
    ) -> Any:
        target_uri_str = str(target_uri)
        if tenant.shared_inbox_uri and str(target_uri_str).startswith(tenant.shared_inbox_uri):
            raise NotImplementedError("Shared inbox does not support POST requests yet")
        store = tenant.public_store
        # All POST requests must be authenticated
        if principal is None:
            raise NotAuthorizedException("Not authenticated")
        target = await self._dereference(store, target_uri)
        if not target:
            raise NotFoundException(target_uri)
        # Boxes must be collections
        if not has_value(target, "type", "OrderedCollection"):
            raise InvalidResourceTypeException(target.get("type"), "OrderedCollection")
        # Found a box, now find the box owner
        box_owner_uri = target.get("attributedTo")
        if box_owner_uri is None or not isinstance(box_owner_uri, str):
            raise ResourceOwnerException("No owner for box")
        box_owner = await self._dereference(store, box_owner_uri)
        if not box_owner:
            raise ResourceOwnerException("Unknown box owner")
        # Determine the type of box and dispatch accordingly
        if target_uri_str == box_owner.get("inbox"):
            decision = await self._authorizer.is_post_authorized(
                tenant, principal, "inbox", target_uri_str
            )
            if decision.authorized:
                return await self._process_inbox(
                    tenants, tenant, principal, resource, cast(APActor, box_owner)
                )
            else:
                raise NotAuthorizedException(decision.reason)
        elif target_uri_str == box_owner.get("outbox"):
            decision = await self._authorizer.is_post_authorized(
                tenant, principal, "outbox", target_uri_str
            )
            if decision.authorized:
                return await self._process_outbox(
                    tenants, tenant, principal, resource, cast(APActor, box_owner)
                )
            else:
                raise NotAuthorizedException(decision.reason)
        else:
            raise InvalidResourceException(target.get("type"), "Unsupported box type")

    async def _process_inbox(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        activity: JSONObject,
        box_owner: APActor,
    ) -> None:
        store = tenant.public_store
        self._validator.validate(activity)
        decision = await self._authorizer.is_activity_authorized(tenant, principal, activity)
        if not decision.authorized:
            raise NotAuthorizedException(decision.reason)
        if log.isEnabledFor(logging.DEBUG):
            log.debug(f"Inbox: activity={activity.get('type')}")
        log.info(f"Inbox: box={activity.get('id')}, activity_type={activity.get('type')}")
        await store.put(activity)
        await self._put_collection_item(store, box_owner["inbox"], resource_id(activity))
        if has_value(activity, "type", "Follow"):
            return await self._process_inbox_follow(tenants, tenant, principal, box_owner, activity)
        elif has_value(activity, "type", "Accept"):
            return await self._process_inbox_accept(tenants, tenant, principal, box_owner, activity)
        elif has_value(activity, "type", "Reject"):
            return await self._process_inbox_reject(tenants, tenant, principal, box_owner, activity)
        elif has_value(activity, "type", "Like"):
            return await self._process_inbox_like(tenants, tenant, principal, box_owner, activity)
        elif has_value(activity, "type", "Create"):
            return await self._process_inbox_create(tenants, tenant, principal, box_owner, activity)
        elif has_value(activity, "type", "Undo"):
            return await self._process_inbox_undo(tenants, tenant, principal, box_owner, activity)
        elif has_value(activity, "type", "Announce"):
            return await self._process_inbox_announce(
                tenants, tenant, principal, box_owner, activity
            )
        else:
            raise NotImplementedError(f"Unsupported activity type: {activity.get('type')}")

    async def _put_collection_item(
        self,
        store: ResourceStore,
        collection_uri: str,
        item_uri: str,
        prepend=True,
        allow_dups=False,
    ):
        collection = await self._dereference(store, collection_uri)
        if not collection:
            raise ValueError(f"Unknown collection: {collection_uri}")
        items_key = (
            "orderedItems" if has_value(collection, "type", "OrderedCollection") else "items"
        )
        if items := cast(list, collection.get(items_key)):
            if isinstance(items, list):
                if not allow_dups and item_uri in items:
                    return
                if prepend:
                    items.insert(0, item_uri)
                else:
                    items.append(item_uri)
        else:
            items = [item_uri]
            collection[items_key] = items
        collection["totalItems"] = len(items)
        await store.put(collection)

    async def _remove_collection_item(
        self, store: ResourceStore, collection_uri: str, item_uri: str
    ):
        collection = await self._dereference(store, collection_uri)
        if not collection:
            raise ValueError(f"Unknown collection: {collection_uri}")
        items_key = (
            "orderedItems" if has_value(collection, "type", "OrderedCollection") else "items"
        )
        if items := collection.get(items_key):
            if isinstance(items, list):
                if item_uri in items:
                    items.remove(item_uri)
        await store.put(collection)

    async def _process_inbox_follow(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        """The actor is requesting to follow the box owner."""
        actor_uri = resource_id(activity.get("actor"))
        # TODO Does the authorization framework handle this already?
        self._assert_authorized_actor(principal, actor_uri)
        if resource_id(activity.get("object")) != box_owner.get("id"):
            raise InvalidRequestException("Mismatch between object and box owner")
        if actor_uri == box_owner.get("id"):
            raise InvalidRequestException("Cannot follow self")
        collection_uri = box_owner.get("followers")
        if not collection_uri:
            raise NotImplementedError("Following not supported")
        store = tenant.public_store
        await self._put_collection_item(store, collection_uri, resource_id(actor_uri))
        # TODO Make auto-accept configurable
        # TODO need a way to identify pending follow requests in store
        log.info(f"Sending Accept to {actor_uri}")
        await self._process_outbox_internal(
            tenant,
            tenants,
            box_owner,
            {
                "@context": "https://www.w3.org/ns/activitystreams",
                "id": self._generate_id("accept", box_owner),
                "type": "Accept",
                "to": actor_uri,
                "actor": box_owner.get("id"),
                "object": activity,
            },
        )

    async def _process_inbox_accept(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        """A remote actor has accepted our follow request."""
        actor_uri = resource_id(activity.get("actor"))
        # TODO Does the authorization framework handle this already?
        self._assert_authorized_actor(principal, actor_uri)
        accepted_activity_uri = resource_id(activity.get("object"))
        store = tenant.public_store
        if accepted_activity := await self._dereference(store, accepted_activity_uri):
            if not is_type(accepted_activity, "Follow"):
                raise InvalidRequestException("Accepting non-Follow object")
            following_uri = box_owner.get("following")
            if not following_uri:
                raise NotImplementedError("Following not supported")
            await self._put_collection_item(
                store, following_uri, resource_id(accepted_activity["object"])
            )
        else:
            raise InvalidResourceException("Unknown accepted object")

    async def _process_inbox_reject(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        """A remote actor has rejected our follow request."""
        actor_uri = resource_id(activity.get("actor"))
        # TODO Does the authorization framework handle this already?
        self._assert_authorized_actor(principal, actor_uri)
        rejected_activity_uri = resource_id(activity.get("object"))
        store = tenant.public_store
        if rejected_activity := await self._dereference(store, rejected_activity_uri):
            if not is_type(rejected_activity, "Follow"):
                raise InvalidRequestException("Accepting non-Follow object")
            following_uri = box_owner.get("following")
            if not following_uri:
                raise NotImplementedError("Following not supported")
            await self._remove_collection_item(
                store, following_uri, resource_id(rejected_activity["object"])
            )
        else:
            raise InvalidResourceException("Unknown accepted object")

    def _assert_authorized_actor(self, principal: Identity | None, actor_uri):
        if principal is None or actor_uri != principal.uri:
            raise NotAuthorizedException()

    async def _process_inbox_like(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        store = tenant.public_store
        self._assert_authorized_actor(principal, activity.get("actor"))
        liked_object_uri = resource_id(activity.get("object"))
        if liked_object := await store.get(liked_object_uri):
            collection_uri = cast(URI, liked_object["likes"])
            await self._put_collection_item(
                store, collection_uri, resource_id(activity.get("actor"))
            )
        else:
            raise InvalidResourceException("Unknown liked object")

    async def _process_inbox_create(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        store = tenant.public_store
        activity_object = activity["object"]
        if isinstance(activity_object, Mapping):
            activity["object"] = resource_id(activity_object)
            await store.put(activity_object)
            await store.put(activity)

    async def _process_inbox_undo(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        # TODO If only URI retrieve remote object
        if resource_get(activity, "object", "type") == "Follow":
            return await self._process_undo_follow(tenant.public_store, box_owner, activity)
        elif resource_get(activity, "object", "type") == "Like":
            await self._process_inbox_undo_like(tenants, tenant, principal, box_owner, activity)
        else:
            raise NotImplementedError("Undo not supported for this object type")

    async def _process_undo_follow(
        self, store: ResourceStore, box_owner: APActor, activity: JSONObject
    ) -> None:
        followed_uri = resource_id(resource_get(activity, "object", "object"))
        if followed_uri is None:
            raise InvalidRequestException("Request has no activity to undo")
        followed_object = cast(APActor, await self._dereference(store, followed_uri))
        if followed_object is None:
            raise InvalidResourceException("Unknown box owner")
        followers_uri = followed_object["followers"]
        if followers_uri is None:
            raise InvalidResourceException("No followers collection")
        await self._remove_collection_item(store, followers_uri, resource_id(activity.get("actor")))
        await self._remove_collection_item(store, box_owner["following"], followed_uri)

    async def _process_inbox_undo_like(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        liked_object_uri = resource_id(resource_get(activity, "object", "object"))
        store = tenant.public_store
        if liked_object := await store.get(liked_object_uri):
            if collection_uri := cast(URI, liked_object["likes"]):
                await self._remove_collection_item(
                    store, collection_uri, resource_id(activity.get("actor"))
                )
                return
        raise InvalidRequestException("Unable to undo like")

    async def _process_inbox_undo_announce(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        announced_object_uri = resource_id(resource_get(activity, "object", "object"))
        store = tenant.public_store
        if announced_object := await store.get(announced_object_uri):
            if collection_uri := cast(URI | None, announced_object.get("shares", None)):
                await self._remove_collection_item(
                    store, collection_uri, resource_id(activity.get("actor"))
                )
        raise InvalidRequestException("Unable to undo announce")

    async def _process_inbox_announce(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        if "object" not in activity:
            raise InvalidRequestException("Missing object in announce")
        announced_object_uri = resource_id(activity["object"])
        announced_object = await self._dereference(tenant.public_store, announced_object_uri)
        if announced_object is None:
            raise InvalidRequestException("Unknown announced object")
        if "shares" not in announced_object:
            shares = {
                "id": f"{announced_object['id']}/shares",
                "type": "OrderedCollection",
                "attributedTo": announced_object.get("attributedTo", box_owner["id"]),
                "totalItems": 0,
                "orderedItems": [],
            }
        else:
            shares_uri = resource_id(announced_object.get("shares"))
            shares = await self._safe_dereference(tenant.public_store, shares_uri)
        items_key = "orderedItems" if any("Ordered" in t for t in get_types(shares)) else "items"
        shared_items = shares.get(items_key, [])
        if activity["id"] not in shared_items:
            shared_items.append(activity["id"])
            shares["totalItems"] = shares.get("totalItems", 0) + 1
        await tenant.public_store.put(shares)
        announced_object["shares"] = shares["id"]
        await tenant.public_store.put(announced_object)

    def _generate_id(self, subpath: str, actor: APActor) -> str:
        return f"{actor.get('id')}/{subpath}/{uuid.uuid4()}"

    @staticmethod
    def _merge_audiences(activity: JSONObject):
        object_ = cast(JSONObject, activity["object"])
        to_audience = set(get_list(activity, "to") + get_list(object_, "to"))
        cc_audience = set(get_list(activity, "cc") + get_list(object_, "cc")) - to_audience

        bto_audience = (
            set(get_list(activity, "bto") + get_list(object_, "bto")) - to_audience - cc_audience
        )
        bcc_audience = (
            set(get_list(activity, "bcc") + get_list(object_, "bcc"))
            - to_audience
            - cc_audience
            - bto_audience
        )

        audience = set(get_list(activity, "audience") + get_list(object_, "audience"))
        audience -= to_audience | cc_audience | bto_audience | bcc_audience

        activity["to"] = list(to_audience)
        activity["cc"] = list(cc_audience)
        activity["bto"] = list(bto_audience)
        activity["bcc"] = list(bcc_audience)
        activity["audience"] = list(audience)
        object_["to"] = list(to_audience)
        object_["cc"] = list(cc_audience)
        object_["bto"] = list(bto_audience)
        object_["bcc"] = list(bcc_audience)
        object_["audience"] = list(audience)

    async def _process_outbox_internal(
        self,
        tenant: Tenant,
        all_tenants: Mapping[str, Tenant],
        box_owner: APActor,
        activity: JSONObject,
    ) -> str | None:
        outbox_uri = box_owner.get("outbox")
        if not outbox_uri:
            raise InvalidResourceException("Box owner has no outbox")
        store = tenant.public_store
        activity_id = (
            f"{activity['actor']}/{"_".join(map(str, get_list(activity, "type")))}/{uuid.uuid4()}"
        )
        activity["id"] = activity_id
        if has_value(activity, "type", "Create"):
            self._merge_audiences(activity)
            object_ = activity["object"]
            if isinstance(object_, Mapping):
                # Always assign an URI to the object for now.
                # TODO: check the object for an "attributedTo" the posting actor.
                # This allows "announcing" an external create.
                if "@context" not in object_:
                    object_["@context"] = "https://www.w3.org/ns/activitystreams"
                object_uri = f"{activity['actor']}/{get_types(object_)[0].lower()}/{uuid.uuid4()}"
                object_["id"] = object_uri
                if "attributedTo" not in object_:
                    object_["attributedTo"] = activity["actor"]
                await store.put(object_)
                activity["object"] = resource_id(object_)
                await store.put(activity)
                await self._put_collection_item(store, outbox_uri, activity_id)
        else:
            try:
                if has_value(activity, "type", "Delete"):
                    if resource := await self._dereference(
                        store, cast(str, resource_id(activity["object"]))
                    ):
                        await store.put(
                            {
                                "@context": "https://www.w3.org/ns/activitystreams",
                                "id": resource["id"],
                                "type": "Tombstone",
                                "audience": "as:Public",
                                "formerType": resource["type"],
                                "deleted": datetime.now().isoformat(),
                            }
                        )
                    else:
                        log.warning(
                            f"Unable to dereference object for delete: {activity['object']}"
                        )
                elif has_value(activity, "type", "Update"):
                    if object_ := activity.get("object"):
                        if isinstance(object_, Mapping):
                            await store.put(object_)
                            activity["object"] = resource_id(object_)
                            await store.put(activity)
                elif has_value(activity, "type", "Block"):
                    blocks = await tenant.private_store.query_one(
                        {
                            "type": FIRM_NS.Blocks.value,
                            "attributedTo": tenant.prefix,
                        }
                    )
                    if not blocks:
                        blocks = {
                            "id": f"{tenant.prefix}/blocks",
                            "type": FIRM_NS.Blocks.value,
                            "attributedTo": tenant.prefix,
                            FIRM_NS.blockedActor.value: [
                                resource_id(activity["object"]),
                            ],
                        }
                    else:
                        blocked_actors = blocks.get(FIRM_NS.blockedActor.value, [])
                        if isinstance(blocked_actors, list):
                            blocked_actors.append(resource_id(activity["object"]))
                    await tenant.private_store.put(blocks)
                elif has_value(activity, "type", "Add"):
                    # Get the target collection
                    # insert the object into the collection at the front
                    # save the collection
                    target = activity.get("target")
                    if isinstance(target, str):
                        target = await self._safe_dereference_or_uri(store, target)
                    if not target or not isinstance(target, Mapping):
                        raise InvalidResourceException("Invalid target collection")
                    if "id" not in target:
                        raise InvalidRequestException("Target collection has no ID")
                    if "items" not in target and "orderedItems" not in target:
                        raise InvalidResourceException("Target collection has no items property")
                    if "object" not in activity:
                        raise InvalidRequestException("Missing object in Add")
                    object_ = activity["object"]
                    if isinstance(object_, str):
                        object_ = await self._safe_dereference_or_uri(store, object_)
                    if not object_ or not isinstance(object_, Mapping):
                        raise InvalidRequestException("Invalid object to add")
                    # Add the object to the collection
                    if "id" not in object_:
                        raise InvalidResourceException("Object has no ID")
                    if "attributedTo" not in object_:
                        object_["attributedTo"] = activity.get("actor", tenant.prefix)
                    await store.put(object_)
                    # Add the object to the collection
                    # TODO find a way to make the typing cleaner
                    if "items" in target:
                        cast(list, target["items"]).append(resource_id(object_))
                    elif "orderedItems" in target:
                        cast(list, target["orderedItems"]).insert(0, resource_id(object_))
                    target["totalItems"] = len(cast(list, target.get("items", []))) + len(
                        cast(list, target.get("orderedItems", []))
                    )
                    await store.put(target)
                    activity["target"] = resource_id(target)
                    activity["object"] = resource_id(object_)
                    # Save the activity
                    await store.put(activity)
                elif has_value(activity, "type", "Remove"):
                    target = activity.get("target")
                    if isinstance(target, str):
                        target = await self._safe_dereference_or_uri(store, target)
                    if not target or not isinstance(target, Mapping):
                        raise InvalidResourceException("Invalid target collection")
                    if "id" not in target:
                        raise InvalidResourceException("Target collection has no ID")
                    if "items" not in target and "orderedItems" not in target:
                        raise InvalidResourceException("Target collection has no items property")
                    if "object" not in activity:
                        raise InvalidRequestException("Missing object in Remove")
                    object_ = activity["object"]
                    if isinstance(object_, str):
                        object_ = await self._safe_dereference_or_uri(store, object_)
                    if not object_ or not isinstance(object_, Mapping):
                        raise InvalidRequestException("Invalid object to remove")
                    # Remove the object from the collection
                    if "id" not in object_:
                        raise InvalidResourceException("Object has no ID")
                    if "items" in target:
                        if resource_id(object_) in cast(list, target["items"]):
                            cast(list, target["items"]).remove(resource_id(object_))
                    elif "orderedItems" in target:
                        if resource_id(object_) in cast(list, target["orderedItems"]):
                            cast(list, target["orderedItems"]).remove(resource_id(object_))
                    else:
                        raise InvalidResourceException("Target collection has no items")
                    target["totalItems"] = len(cast(list, target.get("items", []))) + len(
                        cast(list, target.get("orderedItems", []))
                    )
                    await store.put(target)
                    activity["target"] = resource_id(target)
                    activity["object"] = resource_id(object_)
                    # Save the activity
                    await store.put(activity)
                elif has_value(activity, "type", "Like"):
                    liked_object_uri = resource_id(activity.get("object"))
                    if liked_object := await store.get(liked_object_uri):
                        likes_collection_uri = cast(URI, liked_object.get("likes"))
                        if likes_collection_uri:
                            await self._put_collection_item(
                                store, likes_collection_uri, resource_id(activity.get("actor"))
                            )
                        # Get actor's liked collection and add the liked object to it
                        actor = await store.get(str(activity["actor"]))
                        if actor and "liked" in actor:
                            liked_collection_uri = resource_id(actor.get("liked"))
                            if liked_collection_uri:
                                await self._put_collection_item(
                                    store, liked_collection_uri, liked_object_uri
                                )
                    else:
                        raise InvalidRequestException("Unknown liked object")
                elif has_value(activity, "type", "Undo"):
                    # implement undo for follow, like, announce
                    if resource_get(activity, "object", "type") == "Follow":
                        await self._process_undo_follow(store, box_owner, activity)
                    # TODO Implement outbox undo like
                    # elif resource_get(activity, "object", "type") == "Like":
                    #     await self._process_inbox_undo_like(request, activity)
                    # TODO Implement outbox undo announce
                    # elif resource_get(activity, "object", "type") == "Announce":
                    #     await self._process_inbox_undo_announce(request, activity)
                else:
                    ...
            finally:
                # TODO Implement other outbox activity types
                await store.put(activity)
        await self._put_collection_item(store, outbox_uri, resource_id(activity))
        # TODO Process activity
        await self._delivery_service.deliver(tenant, all_tenants, activity)
        return activity_id

    async def _process_outbox(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        activity: JSONObject,
        box_owner: APActor,
    ) -> str | None:
        # TODO Use JSONObject?
        self._validator.validate(activity)
        if "@context" not in activity:
            activity["@context"] = "https://www.w3.org/ns/activitystreams"
        for activity_type in get_types(activity):
            if activity_type in ACTIVITIES_REQUIRING_OBJECT:
                if "object" not in activity:
                    raise InvalidRequestException("Missing object")
            if activity_type in ACTIVITIES_REQUIRING_TARGET:
                if "target" not in activity:
                    raise InvalidRequestException("Missing target")
        decision = await self._authorizer.is_activity_authorized(tenant, principal, activity)
        if not decision.authorized:
            raise NotAuthorizedException(decision.reason)
        actor_uri = box_owner["id"]
        activity["id"] = f"{actor_uri}/{'_'.join(get_types(activity)).lower()}-{uuid.uuid4()}"
        # Fill in missing fields
        if "actor" not in activity:
            activity["actor"] = actor_uri
        log.info(f"Outbox activity: {activity.get('type')}")
        return await self._process_outbox_internal(
            tenant,
            tenants,
            box_owner,
            activity,
        )


class ActivityPubService:
    def __init__(
        self,
        authorizer: AuthorizationService,
        delivery_service: DeliveryService,
        validator: Validator = NoOpValidator(),
    ) -> None:
        self._handler = ActivityPubTenant(authorizer, delivery_service, validator)

    async def process_get(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        resource_uri: Url,
        options: Mapping[str, Any] | None = None,
    ) -> JSONObject:
        return await self._handler._process_get(tenants, tenant, principal, resource_uri, options)

    async def process_post(
        self,
        tenants: Mapping[str, Tenant],
        tenant: Tenant,
        principal: Identity | None,
        target_uri: Url,
        resource: JSONObject,
    ) -> str | None:
        return await self._handler._process_post(
            tenants=tenants,
            tenant=tenant,
            principal=principal,
            target_uri=target_uri,
            resource=resource,
        )
