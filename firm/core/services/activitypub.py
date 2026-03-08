import logging
import uuid
from datetime import datetime
from http import HTTPStatus
from typing import Mapping, cast
from urllib.parse import parse_qs

from firm.core.interfaces import (
    FIRM_NS,
    JSON,
    URI,
    APActor,
    AuthorizationService,
    DeliveryService,
    HttpException,
    HttpRequest,
    HttpResponse,
    JSONObject,
    JsonResponse,
    NoOpValidator,
    PlainTextResponse,
    ResourceStore,
    Tenant,
    Url,
    Validator,
)
from firm.core.util import (
    ACTIVITIES_REQUIRING_OBJECT,
    ACTIVITIES_REQUIRING_TARGET,
    get_types,
    has_value,
    is_public,
    is_type,
    log,
    resource_get,
    resource_id,
)

OK = PlainTextResponse("", 200, reason_phrase="OK")


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
            return self._safe_dereference(store, url)
        except Exception:
            return str(url)

    async def serialize(self, store: ResourceStore, resource: JSON) -> JSON:
        """Embed specific resources to match typical AP expectations."""
        if not isinstance(resource, dict):
            return resource
        resource_types = get_types(resource)
        if (
            "OrderedCollection" in resource_types
            or "OrderedCollectionPage" in resource_types
            or "Collection" in resource_types
            or "CollectionPage" in resource_types
        ):
            # Keep it simple for now
            items_key = "items" if "items" in resource else "orderedItems"
            items = [
                await self.serialize(
                    store,
                    await self._safe_dereference_or_uri(store, i) if isinstance(i, str) else i,
                )
                for i in cast(list, resource.get(items_key, []))
            ]
            # Embed activity objects
            for item in items:
                if (
                    isinstance(item, Mapping)
                    and "actor" in item
                    and isinstance(item.get("object"), str)
                ):
                    item["object"] = await self._dereference(store, cast(str, item["object"]))
            # TODO Add more general support for empty array serialization
            if not items:
                if items_key in resource:
                    del resource[items_key]
            else:
                resource[items_key] = items
        if "Create" in resource_types or "Update" in resource_types:
            if isinstance(resource.get("object"), str):
                obj = await self._safe_dereference(store, cast(str, resource["object"]))
                resource["object"] = obj
                for prop in ["likes", "shares"]:
                    if prop in obj:
                        collection = await self._safe_dereference(store, cast(str, obj[prop]))
                        collection.pop("items")
                        collection.pop("attributedTo")
                        obj[prop] = collection
        return resource

    @staticmethod
    def _get_query_param(params: Mapping[str, list[str]], key: str, default_value: str) -> str:
        values = params.get(key)
        if not values or len(values) == 0:
            return default_value
        return values[0]

    async def _get_shared_inbox(self, tenant: Tenant, request: HttpRequest) -> HttpResponse:
        """Handle requests to the shared inbox."""
        if request.auth is None:
            raise HttpException(HTTPStatus.FORBIDDEN, "Not authenticated")
        store = tenant.public_store
        default_page_Size = 20
        # TODO Implement pagination
        # This is brute force and limited for now, but we can optimize later
        # It's primarily here for Flowz demonstration purposes
        query_params = parse_qs(request.url.query)
        path_parts = request.url.path.split("/")
        federated = len(path_parts) == 3 and path_parts[-1] == "federated"
        offset_param = self._get_query_param(query_params, "offset", "")
        limit = int(self._get_query_param(query_params, "limit", str(default_page_Size)))
        # strip query and fragment
        box_id = f"{request.url.scheme}://{request.url.netloc}{request.url.path}"
        if offset_param == "":
            return JsonResponse(
                {
                    "@context": "https://www.w3.org/ns/activitystreams",
                    "id": box_id,
                    "type": "OrderedCollection",
                    "first": f"{box_id}?offset=0",
                },
                status_code=200,
                headers={"Content-Type": "application/activity+json"},
            )
        else:
            offset = int(offset_param)
            all_public_activities: list[JSONObject] = []
            if federated:
                # This is obviously not scalable, but for demonstration purposes
                tenants = request.app.state.tenants
                for tenant in tenants.values():
                    all_public_activities.extend(
                        a
                        for a in await store.query(
                            {},
                        )
                        if is_public(a)
                    )
            else:
                all_public_activities.extend(
                    a
                    for a in await store.query(
                        {
                            # "actor": {"$exists": True},
                        },
                    )
                    if "actor" in a
                )

            items = all_public_activities[offset:limit]

            page: JSONObject = {
                "@context": "https://www.w3.org/ns/activitystreams",
                "id": box_id,
                "type": "OrderedCollectionPage",
                "totalItems": len(items),
                "orderedItems": items,
            }

            if offset > 0:
                page["first"] = f"{box_id}?offset=0"
            if offset >= limit:
                page["prev"] = f"{box_id}?offset={max(0, offset - limit)}"
            if len(items) > 0 and (offset + limit < len(all_public_activities)):
                page["next"] = f"{box_id}?offset={int(offset) + limit}"

            return JsonResponse(
                cast(JSONObject, await self.serialize(store, page)),
                status_code=200,
                headers={"Content-Type": "application/activity+json"},
            )

    async def _process_get(self, request: HttpRequest) -> HttpResponse:
        tenant = request.state.tenant
        if tenant.shared_inbox_uri and str(request.url).startswith(tenant.shared_inbox_uri):
            return await self._get_shared_inbox(tenant, request)
        store = tenant.public_store
        if resource := await self._dereference(store, request.url):
            decision = await self._authorizer.is_get_authorized(
                request.state.tenant, request.auth, resource
            )
            if decision.authorized:
                resource = await self.serialize(store, resource)
                status_code = 200
                if resource.get("type") == "Tombstone":
                    status_code = HTTPStatus.GONE
                return JsonResponse(
                    resource,
                    status_code=status_code,
                    headers={"Content-Type": "application/activity+json"},
                )
            else:
                raise HttpException(decision.status_code, decision.reason)
        else:
            raise HttpException(HTTPStatus.NOT_FOUND)

    async def _process_post(self, request: HttpRequest) -> HttpResponse:
        tenant = request.state.tenant
        if tenant.shared_inbox_uri and str(request.url).startswith(tenant.shared_inbox_uri):
            raise HttpException(
                HTTPStatus.NOT_IMPLEMENTED, "Shared inbox does not support POST requests yet"
            )
        store = tenant.public_store
        # All POST requests must be authenticated
        if request.auth is None:
            raise HttpException(HTTPStatus.FORBIDDEN)
        target = await self._dereference(store, request.url)
        if not target:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown target resource")
        # Boxes must be collections
        if not has_value(target, "type", "OrderedCollection"):
            raise HttpException(HTTPStatus.BAD_REQUEST, "Invalid target resource type")
        # Found a box, now find the box owner
        box_owner_uri = target.get("attributedTo")
        if box_owner_uri is None or not isinstance(box_owner_uri, str):
            raise HttpException(HTTPStatus.BAD_REQUEST, "No owner for box")
        box_owner = await self._dereference(store, box_owner_uri)
        if not box_owner:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown box owner")
        # Determine the type of box and dispatch accordingly
        request_url = str(request.url)
        if request_url == box_owner.get("inbox"):
            decision = await self._authorizer.is_post_authorized(
                request.state.tenant, request.auth, "inbox", request_url
            )
            if decision.authorized:
                return await self._process_inbox(request, cast(APActor, box_owner))
            else:
                raise HttpException(decision.status_code, decision.reason)
        elif request_url == box_owner.get("outbox"):
            decision = await self._authorizer.is_post_authorized(
                request.state.tenant, request.auth, "outbox", request_url
            )
            if decision.authorized:
                return await self._process_outbox(request, cast(APActor, box_owner))
            else:
                raise HttpException(decision.status_code, decision.reason)
        else:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unsupported box type")

    async def process_request(self, request: HttpRequest) -> HttpResponse:
        if request.method in ["GET", "HEAD"]:
            return await self._process_get(request)
        elif request.method == "POST":
            return await self._process_post(request)
        else:
            raise HttpException(HTTPStatus.METHOD_NOT_ALLOWED)

    async def _process_inbox(self, request: HttpRequest, box_owner: APActor) -> HttpResponse:
        store = request.state.tenant.public_store
        activity = cast(JSONObject, await request.json())
        self._validator.validate(activity)
        decision = await self._authorizer.is_activity_authorized(
            request.state.tenant, request.auth, activity
        )
        if not decision.authorized:
            raise HttpException(decision.status_code, decision.reason)
        if log.isEnabledFor(logging.DEBUG):
            log.debug(f"Inbox: activity={activity.get('type')}")
        log.info(f"Inbox: box={request.url}, activity_type={activity.get('type')}")
        await store.put(activity)
        await self._put_collection_item(store, box_owner["inbox"], resource_id(activity))
        if has_value(activity, "type", "Follow"):
            return await self._process_inbox_follow(request, box_owner, activity)
        if has_value(activity, "type", "Accept"):
            return await self._process_inbox_accept(request, box_owner, activity)
        elif has_value(activity, "type", "Like"):
            return await self._process_inbox_like(request, box_owner, activity)
        elif has_value(activity, "type", "Create"):
            return await self._process_inbox_create(request, activity)
        elif has_value(activity, "type", "Undo"):
            return await self._process_inbox_undo(request, box_owner, activity)
        elif has_value(activity, "type", "Announce"):
            return await self._process_inbox_announce(request, box_owner, activity)
        else:
            raise HttpException(HTTPStatus.NOT_IMPLEMENTED)

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
        self, request: HttpRequest, box_owner: APActor, activity: JSONObject
    ) -> HttpResponse:
        """The actor is requesting to follow the box owner."""
        actor_uri = resource_id(activity.get("actor"))
        # TODO Does the authorization framework handle this already?
        self._assert_authorized_actor(request, actor_uri)
        if resource_id(activity.get("object")) != box_owner.get("id"):
            raise HttpException(HTTPStatus.BAD_REQUEST, "Mismatch between object and box owner")
        if actor_uri == box_owner.get("id"):
            raise HttpException(HTTPStatus.BAD_REQUEST, "Cannot follow self")
        collection_uri = box_owner.get("followers")
        if not collection_uri:
            raise HttpException(HTTPStatus.NOT_IMPLEMENTED, "Following not supported")
        tenant = request.state.tenant
        store = tenant.public_store
        await self._put_collection_item(store, collection_uri, resource_id(actor_uri))
        # TODO Make auto-accept configurable
        # TODO need a way to identify pending follow requests in store
        log.info(f"Sending Accept to {actor_uri}")
        await self._process_outbox_internal(
            tenant,
            request.app.state.tenants,
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
        return OK

    async def _process_inbox_accept(
        self, request: HttpRequest, box_owner: APActor, activity: JSONObject
    ) -> HttpResponse:
        """A remote actor has accepted our follow request."""
        actor_uri = resource_id(activity.get("actor"))
        # TODO Does the authorization framework handle this already?
        self._assert_authorized_actor(request, actor_uri)
        accepted_activity_uri = resource_id(activity.get("object"))
        store = request.state.tenant.public_store
        if accepted_activity := await self._dereference(store, accepted_activity_uri):
            if not is_type(accepted_activity, "Follow"):
                raise HttpException(HTTPStatus.BAD_REQUEST, "Accepting non-Follow object")
            following_uri = box_owner.get("following")
            if not following_uri:
                raise HttpException(HTTPStatus.NOT_IMPLEMENTED, "Following not supported")
            await self._put_collection_item(
                store, following_uri, resource_id(accepted_activity["object"])
            )
            return OK
        else:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown accepted object")

    def _assert_authorized_actor(self, request, actor_uri):
        if request.auth is None or actor_uri != request.auth.uri:
            raise HttpException(HTTPStatus.FORBIDDEN, "Not authorized")

    async def _process_inbox_like(
        self, request: HttpRequest, box_owner: APActor, activity: JSONObject
    ) -> HttpResponse:
        store = request.state.tenant.public_store
        self._assert_authorized_actor(request, activity.get("actor"))
        liked_object_uri = resource_id(activity.get("object"))
        if liked_object := await store.get(liked_object_uri):
            collection_uri = cast(URI, liked_object["likes"])
            await self._put_collection_item(
                store, collection_uri, resource_id(activity.get("actor"))
            )
            return OK
        else:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown liked object")

    async def _process_inbox_create(
        self, request: HttpRequest, activity: JSONObject
    ) -> HttpResponse:
        store = request.state.tenant.public_store
        activity_object = activity["object"]
        if isinstance(activity_object, Mapping):
            activity["object"] = resource_id(activity_object)
            await store.put(activity_object)
            await store.put(activity)
        return OK

    async def _process_inbox_undo(
        self, request: HttpRequest, box_owner: APActor, activity: JSONObject
    ) -> HttpResponse:
        # TODO If only URI retrieve remote object
        if resource_get(activity, "object", "type") == "Follow":
            return await self._process_undo_follow(
                request.state.tenant.public_store, box_owner, activity
            )
        elif resource_get(activity, "object", "type") == "Like":
            return await self._process_inbox_undo_like(request, activity)
        else:
            raise HttpException(HTTPStatus.NOT_IMPLEMENTED)

    async def _process_undo_follow(
        self, store: ResourceStore, box_owner: APActor, activity: JSONObject
    ) -> HttpResponse:
        followed_uri = resource_id(resource_get(activity, "object", "object"))
        if followed_uri is None:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Request has not activity to undo")
        followed_object = cast(APActor, await self._dereference(store, followed_uri))
        if followed_object is None:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown box owner")
        followers_uri = followed_object["followers"]
        if followers_uri is None:
            raise HttpException(HTTPStatus.BAD_REQUEST, "No followers collection")
        await self._remove_collection_item(store, followers_uri, resource_id(activity.get("actor")))
        await self._remove_collection_item(store, box_owner["following"], followed_uri)
        return OK

    async def _process_inbox_undo_like(
        self, request: HttpRequest, activity: JSONObject
    ) -> HttpResponse:
        liked_object_uri = resource_id(resource_get(activity, "object", "object"))
        store = request.state.tenant.public_store
        if liked_object := await store.get(liked_object_uri):
            if collection_uri := cast(URI, liked_object["likes"]):
                await self._remove_collection_item(
                    store, collection_uri, resource_id(activity.get("actor"))
                )
                return OK
        raise HttpException(HTTPStatus.BAD_REQUEST, "Unable to undo like")

    async def _process_inbox_undo_announce(
        self, request: HttpRequest, activity: JSONObject
    ) -> HttpResponse:
        announced_object_uri = resource_id(resource_get(activity, "object", "object"))
        store = request.state.tenant.public_store
        if announced_object := await store.get(announced_object_uri):
            if collection_uri := cast(URI | None, announced_object.get("shares", None)):
                await self._remove_collection_item(
                    store, collection_uri, resource_id(activity.get("actor"))
                )
                return OK
        raise HttpException(HTTPStatus.BAD_REQUEST, "Unable to undo announce")

    async def _process_inbox_announce(
        self, request: HttpRequest, box_owner: APActor, activity: JSONObject
    ) -> HttpResponse:
        if "object" not in activity:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Missing object in announce")
        announced_object_uri = resource_id(activity["object"])
        tenant = request.state.tenant
        announced_object = await self._dereference(tenant.public_store, announced_object_uri)
        if announced_object is None:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown announced object")
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
        return OK

    def _generate_id(self, subpath: str, actor: APActor) -> str:
        return f"{actor.get('id')}/{subpath}/{uuid.uuid4()}"

    async def _process_outbox_internal(
        self,
        tenant: Tenant,
        all_tenants: Mapping[str, Tenant],
        box_owner: APActor,
        activity: JSONObject,
    ) -> None:
        outbox_uri = box_owner.get("outbox")
        if not outbox_uri:
            raise HttpException(HTTPStatus.INTERNAL_SERVER_ERROR, "Box owner has no outbox")
        store = tenant.public_store
        if has_value(activity, "type", "Create"):
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
                        raise HttpException(HTTPStatus.BAD_REQUEST, "Invalid target collection")
                    if "id" not in target:
                        raise HttpException(HTTPStatus.BAD_REQUEST, "Target collection has no ID")
                    if "items" not in target and "orderedItems" not in target:
                        raise HttpException(
                            HTTPStatus.BAD_REQUEST, "Target collection has no items"
                        )
                    if "object" not in activity:
                        raise HttpException(HTTPStatus.BAD_REQUEST, "Missing object in Add")
                    object_ = activity["object"]
                    if isinstance(object_, str):
                        object_ = await self._safe_dereference_or_uri(store, object_)
                    if not object_ or not isinstance(object_, Mapping):
                        raise HttpException(HTTPStatus.BAD_REQUEST, "Invalid object to add")
                    # Add the object to the collection
                    if "id" not in object_:
                        raise Exception(HTTPStatus.BAD_REQUEST, "Object has no ID")
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
                        raise HttpException(HTTPStatus.BAD_REQUEST, "Invalid target collection")
                    if "id" not in target:
                        raise HttpException(HTTPStatus.BAD_REQUEST, "Target collection has no ID")
                    if "items" not in target and "orderedItems" not in target:
                        raise HttpException(
                            HTTPStatus.BAD_REQUEST, "Target collection has no items"
                        )
                    if "object" not in activity:
                        raise HttpException(HTTPStatus.BAD_REQUEST, "Missing object in Remove")
                    object_ = activity["object"]
                    if isinstance(object_, str):
                        object_ = await self._safe_dereference_or_uri(store, object_)
                    if not object_ or not isinstance(object_, Mapping):
                        raise HttpException(HTTPStatus.BAD_REQUEST, "Invalid object to remove")
                    # Remove the object from the collection
                    if "id" not in object_:
                        raise Exception(HTTPStatus.BAD_REQUEST, "Object has no ID")
                    if "items" in target:
                        if resource_id(object_) in cast(list, target["items"]):
                            cast(list, target["items"]).remove(resource_id(object_))
                    elif "orderedItems" in target:
                        if resource_id(object_) in cast(list, target["orderedItems"]):
                            cast(list, target["orderedItems"]).remove(resource_id(object_))
                    else:
                        raise HttpException(
                            HTTPStatus.BAD_REQUEST, "Target collection has no items"
                        )
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
                        raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown liked object")
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

    async def _process_outbox(self, request: HttpRequest, box_owner: APActor) -> HttpResponse:
        # TODO Use JSONObject?
        activity = dict(await request.json())
        self._validator.validate(activity)
        if "@context" not in activity:
            activity["@context"] = "https://www.w3.org/ns/activitystreams"
        for activity_type in get_types(activity):
            if activity_type in ACTIVITIES_REQUIRING_OBJECT:
                if "object" not in activity:
                    raise HttpException(HTTPStatus.BAD_REQUEST, "Missing object")
            if activity_type in ACTIVITIES_REQUIRING_TARGET:
                if "target" not in activity:
                    raise HttpException(HTTPStatus.BAD_REQUEST, "Missing target")
        decision = await self._authorizer.is_activity_authorized(
            request.state.tenant, request.auth, activity
        )
        if not decision.authorized:
            raise HttpException(decision.status_code, decision.reason)
        actor_uri = box_owner["id"]
        activity["id"] = f"{actor_uri}/{'_'.join(get_types(activity)).lower()}-{uuid.uuid4()}"
        # Fill in missing fields
        if "actor" not in activity:
            activity["actor"] = actor_uri
        log.info(f"Outbox activity: {activity.get('type')}")
        await self._process_outbox_internal(
            request.state.tenant,
            request.app.state.tenants,
            box_owner,
            activity,
        )
        return PlainTextResponse(
            "Processed",
            200,
            reason_phrase="OK",
            headers={"Location": activity["id"]},
        )


class ActivityPubService:
    def __init__(
        self,
        authorizer: AuthorizationService,
        delivery_service: DeliveryService,
        validator: Validator = NoOpValidator(),
    ) -> None:
        self._handler = ActivityPubTenant(authorizer, delivery_service, validator)

    async def process_request(self, request: HttpRequest) -> HttpResponse:
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                f"Request: {request.method} {request.url} "
                f"authenticated_actor={request.auth.uri if request.auth else 'none'}"
            )
        # TODO Clean up the handler after the refactoring
        return await self._handler.process_request(request)
