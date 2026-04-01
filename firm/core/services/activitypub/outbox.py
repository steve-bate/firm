import logging
from datetime import datetime
from typing import Any, Mapping, cast

from jsonpointer import JsonPointer

from firm.core.interfaces import FIRM_NS, URI, DeliveryService, JSONObject, Tenant
from firm.core.services.activitypub.collections import (
    add_collection_item,
    remove_collection_item,
)
from firm.core.services.activitypub.exceptions import (
    InvalidRequestException,
    InvalidResourceException,
    NotFoundException,
)
from firm.core.services.activitypub.support import (
    FirmBoxContext,
    FirmContext,
    generate_id,
)
from firm.core.util import (
    ACTIVITIES_REQUIRING_OBJECT,
    ACTIVITIES_REQUIRING_TARGET,
    get_id,
    get_list,
    get_types,
    has_value,
    is_type,
    is_type_any,
    resource_get,
    resource_id,
)

from .inbox import _process_undo_follow

log = logging.getLogger(__name__)


def _merge_audiences(activity: JSONObject) -> None:
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

    if to_audience:
        activity["to"] = list(to_audience)
        object_["to"] = list(to_audience)
    if cc_audience:
        activity["cc"] = list(cc_audience)
        object_["cc"] = list(cc_audience)
    if bto_audience:
        activity["bto"] = list(bto_audience)
        object_["bto"] = list(bto_audience)
    if bcc_audience:
        activity["bcc"] = list(bcc_audience)
        object_["bcc"] = list(bcc_audience)
    if audience:
        activity["audience"] = list(audience)
        object_["audience"] = list(audience)


async def outbox_send(
    tenant: Tenant,
    tenants: Mapping[str, Tenant],
    activity: JSONObject,
    outbox_uri: str,
    delivery_service: DeliveryService,
) -> None:
    # Add to outbox collection, then deliver to recipients
    object_ = activity.get("object")
    if object_ and isinstance(object_, Mapping):
        await tenant.public_store.put(object_)
    await tenant.public_store.put(activity)
    await add_collection_item(tenant.public_store, outbox_uri, resource_id(activity))
    await delivery_service.deliver(tenant, tenants, activity)


async def process_outbox(
    context: FirmBoxContext, activity: JSONObject, delivery_service: DeliveryService
) -> str | None:
    if "@context" not in activity:
        activity["@context"] = "https://www.w3.org/ns/activitystreams"
    for activity_type in get_types(activity):
        if activity_type in ACTIVITIES_REQUIRING_OBJECT:
            if "object" not in activity:
                raise InvalidRequestException("Missing object")
        if activity_type in ACTIVITIES_REQUIRING_TARGET:
            if "target" not in activity:
                raise InvalidRequestException("Missing target")
    log.info(f"Outbox activity: {activity.get('type')}")
    actor_uri = resource_id(context.box_owner)
    activity_id = generate_id(actor_uri, activity)
    activity["id"] = activity_id
    # Fill in missing fields
    if "actor" not in activity:
        activity["actor"] = actor_uri
    if "attributedTo" not in activity:
        activity["attributedTo"] = actor_uri
    if has_value(activity, "type", "Create"):
        await _process_outbox_create(context, activity)
    else:
        try:
            if has_value(activity, "type", "Delete"):
                await _process_outbox_delete(context, activity)
            elif has_value(activity, "type", "Update"):
                await _process_outbox_update(context, activity)
            elif has_value(activity, "type", "Block"):
                await _process_outbox_block(context, activity)
            elif has_value(activity, "type", "Add"):
                await _process_outbox_add(context, activity)
            elif has_value(activity, "type", "Remove"):
                await _process_outbox_remove(context, activity)
            elif has_value(activity, "type", "Like"):
                await _process_outbox_like(context, activity)
            elif has_value(activity, "type", "Undo"):
                await _process_outbox_undo(context, activity)
            elif has_value(activity, "type", "Patch"):
                await _process_outbox_patch(context, activity)
            else:
                # raise NotImplementedError(f"Unsupported activity type: {activity.get('type')}")
                ...
        finally:
            await context.tenant.public_store.put(activity)
    await add_collection_item(context.tenant.public_store, context.box_uri, resource_id(activity))
    await delivery_service.deliver(context.tenant, context.tenants, activity)
    return activity_id


async def _process_outbox_patch(context: FirmContext, activity: JSONObject):
    target_uri = get_id(activity.get("target"))
    if not target_uri:
        raise InvalidRequestException("Missing target for Patch activity")
    patch_ops = activity.get("object")
    if (
        not is_type(activity, "Patch")
        or not isinstance(patch_ops, dict)
        or not is_type(patch_ops, "PatchOperations")
    ):
        raise InvalidRequestException("Invalid object for Patch activity")
    target = await context.dereference(target_uri)
    if not target:
        raise NotFoundException("Unknown target for Patch activity")
    operations = patch_ops.get("operations")
    if not isinstance(operations, list):
        raise InvalidRequestException("Invalid operations for Patch activity")
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise InvalidRequestException("Invalid operation in Patch activity")
        if "op" not in operation or "path" not in operation:
            raise InvalidRequestException("Operation missing required fields in Patch activity")
        path = operation["path"]
        if not path.startswith("/"):
            raise InvalidRequestException("Invalid path in Patch activity, must start with /")
        # Hydrate the target, if necessary
        path_segments = path.strip("/").split("/")
        for segment in path_segments[:-1]:
            current: Any = target
            if segment in target and isinstance(target[segment], str):
                current[segment] = await context.dereference(target[segment], required=True)
                current = current[segment]
        op = operation["op"]
        ptr = JsonPointer(path)
        store = context.tenant.public_store
        if op == "add":
            if "value" not in operation:
                raise InvalidRequestException("Add operation missing value in Patch activity")
            ptr.set(target, operation["value"], inplace=True)
            await store.put(target)
        elif op == "remove":
            parent, token = ptr.to_last(target)
            del parent[token]
            await store.put(target)
        elif op == "replace":
            if "value" not in operation:
                raise InvalidRequestException("Replace operation missing value in Patch activity")
            parent, token = ptr.to_last(target)
            if isinstance(parent, list):
                parent[int(token)] = operation["value"]
            elif isinstance(parent, dict):
                parent[token] = operation["value"]
            else:
                raise InvalidRequestException(
                    f"Unsupported parent type for replace operation in Patch activity: {type(parent)}"
                )
            await store.put(target)
        elif op == "move":
            if "from" not in operation:
                raise InvalidRequestException("Move operation missing from in Patch activity")
            from_ptr = JsonPointer(operation["from"])
            value = from_ptr.get(target)
            from_parent, from_token = from_ptr.to_last(target)
            if isinstance(from_parent, list):
                del from_parent[int(from_token)]
            elif isinstance(from_parent, dict):
                del from_parent[from_token]
            path_ptr = JsonPointer(operation["path"])
            path_ptr.set(target, value, inplace=True)
            await store.put(target)
        elif op == "copy":
            if "from" not in operation:
                raise InvalidRequestException("Move operation missing from in Patch activity")
            from_ptr = JsonPointer(operation["from"])
            value = from_ptr.get(target)
            path_ptr = JsonPointer(operation["path"])
            path_ptr.set(target, value, inplace=True)
            await store.put(target)
        elif op == "test":
            if "value" not in operation:
                raise InvalidRequestException("Replace operation missing value in Patch activity")
            ptr = JsonPointer(operation["path"])
            target_value = ptr.get(target)
            if target_value != operation["value"]:
                raise InvalidRequestException("Test operation failed in Patch activity")
        else:
            raise InvalidRequestException(f"Unsupported operation in Patch activity: {op}")


async def _process_outbox_undo(context: FirmBoxContext, activity: JSONObject) -> None:
    # implement undo for follow, like, announce
    if resource_get(activity, "object", "type") == "Follow":
        await _process_undo_follow(context, activity)  # type: ignore
    # TODO Implement outbox undo like
    # elif resource_get(activity, "object", "type") == "Like":
    #     await self._process_inbox_undo_like(request, activity)
    # TODO Implement outbox undo announce
    # elif resource_get(activity, "object", "type") == "Announce":
    #     await self._process_inbox_undo_announce(request, activity)


async def _process_outbox_like(context: FirmBoxContext, activity: JSONObject) -> None:
    store = context.tenant.public_store
    liked_object_uri = resource_id(activity.get("object"))
    if liked_object := await store.get(liked_object_uri):
        likes_collection_uri = cast(URI, liked_object.get("likes"))
        if likes_collection_uri:
            await add_collection_item(
                store, likes_collection_uri, resource_id(activity.get("actor"))
            )
            # Get actor's liked collection and add the liked object to it
        actor = await store.get(str(activity["actor"]))
        if actor and "liked" in actor:
            liked_collection_uri = resource_id(actor.get("liked"))
            if liked_collection_uri:
                await add_collection_item(store, liked_collection_uri, liked_object_uri)
    else:
        raise InvalidRequestException("Unknown liked object")


async def _process_outbox_remove(context: FirmBoxContext, activity: JSONObject) -> None:
    target = activity.get("target")
    if isinstance(target, str):
        target = await context.dereference(target)
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
        object_ = await context.dereference(object_)
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
    store = context.tenant.public_store
    await store.put(target)
    activity["target"] = resource_id(target)
    activity["object"] = resource_id(object_)
    # Save the activity
    await store.put(activity)


async def _process_outbox_add(context: FirmBoxContext, activity: JSONObject) -> None:
    # Get the target collection
    # insert the object into the collection at the front
    # save the collection
    target = activity.get("target")
    if isinstance(target, str):
        target = await context.dereference(target)
    if not target or not isinstance(target, Mapping):
        raise InvalidResourceException("Invalid target collection")
    if "id" not in target:
        raise InvalidRequestException("Target collection has no ID")
    if "items" not in target and "orderedItems" not in target:
        if is_type(target, "Collection"):
            target["items"] = []
        elif is_type(target, "OrderedCollection"):
            target["orderedItems"] = []
        else:
            raise InvalidResourceException("Target collection has no items property")
    if "object" not in activity:
        raise InvalidRequestException("Missing object in Add")
    object_ = activity["object"]
    if isinstance(object_, str):
        object_ = await context.dereference(object_)
    if not object_ or not isinstance(object_, Mapping):
        raise InvalidRequestException("Invalid object to add")
    # Add the object to the collection
    if "id" not in object_:
        raise InvalidResourceException("Object has no ID")
    if "attributedTo" not in object_:
        object_["attributedTo"] = activity.get("actor", context.tenant.prefix)
    store = context.tenant.public_store
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


async def _process_outbox_block(context, activity):
    blocks = await context.tenant.private_store.query_one(
        {
            "type": FIRM_NS.Blocks.value,
            "attributedTo": context.tenant.prefix,
        }
    )
    if not blocks:
        blocks = {
            "id": f"{context.tenant.prefix}/blocks",
            "type": FIRM_NS.Blocks.value,
            "attributedTo": context.tenant.prefix,
            FIRM_NS.blockedActor.value: [
                resource_id(activity["object"]),
            ],
        }
    else:
        blocked_actors = blocks.get(FIRM_NS.blockedActor.value, [])
        if isinstance(blocked_actors, list):
            blocked_actors.append(resource_id(activity["object"]))
    await context.tenant.private_store.put(blocks)


async def _process_outbox_update(context: FirmBoxContext, activity: JSONObject) -> None:
    if object_ := activity.get("object"):
        if isinstance(object_, Mapping):
            store = context.tenant.public_store
            await store.put(object_)
            activity["object"] = resource_id(object_)
            await store.put(activity)


async def _process_outbox_delete(context, activity):
    if resource := await context.dereference(cast(str, resource_id(activity["object"]))):
        store = context.tenant.public_store
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
        if is_type_any(resource, ["Collection", "OrderedCollection"]):
            log.info(f"Unregistering deleted collection: {resource['id']}")
            collection_index_uri = resource_id(context.box_owner.get(FIRM_NS.collections.value))
            if collection_index_uri:
                await remove_collection_item(
                    store,
                    resource_id(collection_index_uri),
                    resource_id(resource),
                )
    else:
        log.warning(f"Unable to dereference object for delete: {activity['object']}")


async def _process_outbox_create(context: FirmBoxContext, activity: JSONObject) -> None:
    _merge_audiences(activity)
    object_ = activity["object"]
    if isinstance(object_, Mapping):
        # Always assign an URI to the object for now.
        # TODO: check the object for an "attributedTo" the posting actor.
        # This allows "announcing" an external create.
        if "@context" not in object_:
            object_["@context"] = "https://www.w3.org/ns/activitystreams"
        actor_id = resource_id(context.box_owner)
        object_uri = generate_id(actor_id, object_)
        object_["id"] = object_uri
        if "attributedTo" not in object_:
            object_["attributedTo"] = actor_id
        store = context.tenant.public_store
        await store.put(object_)
        activity["object"] = resource_id(object_)
        await store.put(activity)
        outbox_uri = context.box_uri
        await add_collection_item(store, outbox_uri, resource_id(activity))
        if is_type_any(object_, ["Collection", "OrderedCollection"]):
            log.info(f"Registering created collection: {object_['id']}")
            collection_index_uri = get_id(context.box_owner.get(FIRM_NS.collections.value))
            if not collection_index_uri:
                collection_index: JSONObject = {
                    "id": f"{actor_id}/collections",
                    "type": FIRM_NS.collections.value,
                    "attributedTo": actor_id,
                }
                cast(dict, context.box_owner)[FIRM_NS.collections.value] = collection_index["id"]
                await store.put(collection_index)
                await store.put(cast(JSONObject, context.box_owner))
            else:
                collection_index = await context.dereference(collection_index_uri, required=True)
            await add_collection_item(store, resource_id(collection_index), resource_id(object_))
    # # TODO Process activity
    # await self._delivery_service.deliver(tenant, tenants, activity)
    # return activity_id
