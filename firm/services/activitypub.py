import logging
import uuid
from http import HTTPStatus
from typing import Mapping, cast

from firm.interfaces import (
    URI,
    APActor,
    DeliveryService,
    HttpException,
    HttpRequest,
    HttpResponse,
    JSONObject,
    JsonResponse,
    PlainTextResponse,
    ResourceStore,
    Url,
    UrlPrefix,
    get_url_prefix,
)
from firm.util import has_value, log, resource_get, resource_id

OK = PlainTextResponse("", 200, reason_phrase="OK")


class ActivityPubTenant:
    def __init__(
        self, prefix: UrlPrefix, store: ResourceStore, delivery_service: DeliveryService
    ):
        self.prefix = prefix
        self._store = store
        self._delivery_service = delivery_service

    async def _dereference(self, url: Url | str):
        if isinstance(url, Url):
            url = str(url)
        return await self._store.get(url)

    async def _process_get(self, request: HttpRequest) -> HttpResponse:
        if resource := await self._dereference(request.url):
            return JsonResponse(
                resource, headers={"Content-Type": "application/activity+json"}
            )
        else:
            raise HttpException(HTTPStatus.NOT_FOUND)

    async def _process_post(self, request: HttpRequest) -> HttpResponse:
        # All POST requests must be authenticated
        if request.auth is None:
            raise HttpException(HTTPStatus.FORBIDDEN)
        target = await self._dereference(request.url)
        if not target:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown target resource")
        # Boxes must be collections
        if not has_value(target, "type", "OrderedCollection"):
            raise HttpException(HTTPStatus.BAD_REQUEST, "Invalid target resource type")
        # Found a box, now find the box owner
        box_owner_uri = target.get("attributedTo")
        if box_owner_uri is None or not isinstance(box_owner_uri, str):
            raise HttpException(HTTPStatus.BAD_REQUEST, "No owner for box")
        box_owner = await self._dereference(box_owner_uri)
        if not box_owner:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown box owner")
        # Determine the type of box and dispatch accordingly
        request_url = str(request.url)
        if request_url == box_owner.get("inbox"):
            return await self._process_inbox(request, cast(APActor, box_owner))
        elif request_url == box_owner.get("outbox"):
            return await self._process_outbox(request, cast(APActor, box_owner))
        else:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unsupported box type")

    async def process_request(self, request: HttpRequest) -> HttpResponse:
        if request.method == "GET":
            return await self._process_get(request)
        elif request.method == "POST":
            return await self._process_post(request)
        else:
            raise HttpException(HTTPStatus.METHOD_NOT_ALLOWED)

    async def _process_inbox(
        self, request: HttpRequest, box_owner: APActor
    ) -> HttpResponse:
        activity = cast(JSONObject, await request.json())
        if log.isEnabledFor(logging.DEBUG):
            log.debug(f"Inbox: activity={activity.get('type')}")
        log.info(f"Inbox: box={request.url}, activity_type={activity.get('type')}")
        await self._store.put(activity)
        await self._put_collection_item(box_owner["inbox"], resource_id(activity))
        if has_value(activity, "type", "Follow"):
            return await self._process_inbox_follow(request, box_owner, activity)
        elif has_value(activity, "type", "Like"):
            return await self._process_inbox_like(request, box_owner, activity)
        elif has_value(activity, "type", "Create"):
            return await self._process_inbox_create(request, activity)
        elif has_value(activity, "type", "Undo"):
            return await self._process_inbox_undo(request, activity)
        else:
            raise HttpException(HTTPStatus.NOT_IMPLEMENTED)

    async def _put_collection_item(
        self, collection_uri: str, item_uri: str, prepend=True, allow_dups=False
    ):
        collection = await self._dereference(collection_uri)
        if not collection:
            raise ValueError(f"Unknown collection: {collection_uri}")
        items_key = (
            "orderedItems"
            if has_value(collection, "type", "OrderedCollection")
            else "items"
        )
        if items := collection.get(items_key):
            if isinstance(items, list):
                if not allow_dups and item_uri in items:
                    return
                if prepend:
                    items.insert(0, item_uri)
                else:
                    items.append(item_uri)
        else:
            collection[items_key] = [item_uri]
        await self._store.put(collection)

    async def _remove_collection_item(self, collection_uri: str, item_uri: str):
        collection = await self._dereference(collection_uri)
        if not collection:
            raise ValueError(f"Unknown collection: {collection_uri}")
        items_key = (
            "orderedItems"
            if has_value(collection, "type", "OrderedCollection")
            else "items"
        )
        if items := collection.get(items_key):
            if isinstance(items, list):
                if item_uri in items:
                    items.remove(item_uri)
        await self._store.put(collection)

    async def _process_inbox_follow(
        self, request: HttpRequest, box_owner: APActor, activity: JSONObject
    ) -> HttpResponse:
        """The actor is requesting to follow the box owner."""
        actor_uri = resource_id(activity.get("actor"))
        self._assert_authorized_actor(request, actor_uri)
        if resource_id(activity.get("object")) != box_owner.get("id"):
            raise HttpException(
                HTTPStatus.BAD_REQUEST, "Mismatch between object and box owner"
            )
        if actor_uri == box_owner.get("id"):
            raise HttpException(HTTPStatus.BAD_REQUEST, "Cannot follow self")
        collection_uri = box_owner.get("followers")
        if not collection_uri:
            raise HttpException(HTTPStatus.NOT_IMPLEMENTED, "Following not supported")
        await self._put_collection_item(collection_uri, resource_id(actor_uri))
        # TODO Make auto-accept configurable
        # TODO need a way to identify pending follow requests in store
        log.info(f"Sending Accept to {actor_uri}")
        await self._process_outbox_internal(
            box_owner["outbox"],
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

    def _assert_authorized_actor(self, request, actor_uri):
        if request.auth is None or actor_uri != request.auth.uri:
            raise HttpException(HTTPStatus.FORBIDDEN, "Not authorized")

    async def _process_inbox_like(
        self, request: HttpRequest, box_owner: APActor, activity: JSONObject
    ) -> HttpResponse:
        self._assert_authorized_actor(request, activity.get("actor"))
        liked_object_uri = resource_id(activity.get("object"))
        if liked_object := await self._store.get(liked_object_uri):
            collection_uri = cast(URI, liked_object["likes"])
            await self._put_collection_item(
                collection_uri, resource_id(activity.get("actor"))
            )
            return OK
        else:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown liked object")

    async def _process_inbox_create(
        self, request: HttpRequest, activity: JSONObject
    ) -> HttpResponse:
        activity_object = activity["object"]
        if isinstance(activity_object, Mapping):
            activity["object"] = resource_id(activity_object)
            await self._store.put(activity_object)
            await self._store.put(activity)
        return OK

    async def _process_inbox_undo(
        self, request: HttpRequest, activity: JSONObject
    ) -> HttpResponse:
        # TODO If only URI retrieve remote object
        if resource_get(activity, "object", "type") == "Follow":
            return await self._process_inbox_undo_follow(request, activity)
        elif resource_get(activity, "object", "type") == "Like":
            return await self._process_inbox_undo_like(request, activity)
        else:
            raise HttpException(HTTPStatus.NOT_IMPLEMENTED)

    async def _process_inbox_undo_follow(
        self, request: HttpRequest, activity: JSONObject
    ) -> HttpResponse:
        box_owner_uri = resource_id(resource_get(activity, "object", "object"))
        if box_owner_uri is None:
            raise HttpException(
                HTTPStatus.BAD_REQUEST, "Request has not activity to undo"
            )
        box_owner = cast(APActor, await self._dereference(box_owner_uri))
        if box_owner is None:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown box owner")
        collection_uri = box_owner["followers"]
        if collection_uri is None:
            raise HttpException(HTTPStatus.BAD_REQUEST, "No followers collection")
        await self._remove_collection_item(
            collection_uri, resource_id(activity.get("actor"))
        )
        return OK

    async def _process_inbox_undo_like(
        self, request: HttpRequest, activity: JSONObject
    ) -> HttpResponse:
        liked_object_uri = resource_id(resource_get(activity, "object", "object"))
        if liked_object := await self._store.get(liked_object_uri):
            if collection_uri := cast(URI, liked_object["likes"]):
                await self._remove_collection_item(
                    collection_uri, resource_id(activity.get("actor"))
                )
                return OK
        raise HttpException(HTTPStatus.BAD_REQUEST, "Unable to undo like")

    def _generate_id(self, subpath: str, actor: APActor) -> str:
        return f"{actor.get('id')}/{subpath}/{uuid.uuid4()}"

    async def _process_outbox_internal(
        self, outbox_uri: str, activity: JSONObject
    ) -> None:
        if has_value(activity, "type", "Create"):
            activity_object = activity["object"]
            if isinstance(activity_object, Mapping):
                activity["object"] = resource_id(activity_object)
                # Always assign an URI to the object for now.
                # TODO: check the object for an "attributedTo" the posting actor.
                # This allows "announcing" an external create.
                activity_type = str(activity_object.get("type", "object")).lower()
                object_uri = f"{activity['actor']}/{activity_type}/{uuid.uuid4()}"
                activity_object["id"] = object_uri
                activity_object["attributedTo"] = activity["actor"]
                await self._store.put(activity_object)
        else:
            # TODO Implement other outbox activity types
            await self._store.put(activity)
        await self._put_collection_item(outbox_uri, resource_id(activity))
        await self._delivery_service.deliver(activity)

    async def _process_outbox(
        self, request: HttpRequest, box_owner: APActor
    ) -> HttpResponse:
        actor_uri = box_owner["id"]
        activity = dict(await request.json())
        activity["id"] = f"{actor_uri}/{activity['type'].lower()}-{uuid.uuid4()}"
        # Fill in missing fields
        if "actor" not in activity:
            activity["actor"] = actor_uri
        if "@context" not in activity:
            # for Mastodon
            activity["@context"] = "https://www.w3.org/ns/activitystreams"
        log.info(f"Outbox activity: {activity.get('type')}")
        await self._process_outbox_internal(box_owner["outbox"], activity)
        return PlainTextResponse(
            "Processed",
            200,
            reason_phrase="OK",
            headers={"Location": activity["id"]},
        )


class ActivityPubService:
    def __init__(self, tenants: list[ActivityPubTenant] | None = None) -> None:
        self.tenants: dict[str, ActivityPubTenant] = (
            {str(t.prefix): t for t in tenants} if tenants else {}
        )

    def get_tenant(self, prefix: str) -> ActivityPubTenant | None:
        return self.tenants.get(prefix)

    async def process_request(self, request: HttpRequest) -> HttpResponse:
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                f"Request: {request.method} {request.url} "
                f"authenticated_actor={request.auth.uri if request.auth else 'none'}"
            )
        if tenant := self.get_tenant(get_url_prefix(request.url)):
            return await tenant.process_request(request)
        else:
            raise HttpException(HTTPStatus.BAD_REQUEST, "Unknown tenant")
