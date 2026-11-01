import logging
from typing import Mapping

from firm.core.interfaces import JSONObject
from firm.core.services.activitypub.collections import (
    add_collection_item,
    remove_collection_item,
)
from firm.core.services.activitypub.exceptions import (
    InvalidRequestException,
    InvalidResourceException,
)
from firm.core.services.activitypub.support import FirmBoxContext, generate_id
from firm.core.util import get_id, has_value, is_type, resource_get, resource_id

log = logging.getLogger(__name__)


async def process_inbox(
    context: FirmBoxContext,
    activity: JSONObject,
):
    if has_value(activity, "type", "Follow"):
        return await _process_inbox_follow(context, activity)
    elif has_value(activity, "type", "Accept"):
        return await _process_inbox_accept(context, activity)
    elif has_value(activity, "type", "Reject"):
        return await _process_inbox_reject(context, activity)
    elif has_value(activity, "type", "Like"):
        return await _process_inbox_like(context, activity)
    elif has_value(activity, "type", "Create"):
        return await _process_inbox_create(context, activity)
    elif has_value(activity, "type", "Undo"):
        return await _process_inbox_undo(context, activity)
    elif has_value(activity, "type", "Delete"):
        return await _process_inbox_delete(context, activity)
    elif has_value(activity, "type", "Announce"):
        return await _process_inbox_announce(context, activity)
    else:
        raise NotImplementedError(f"Unsupported activity type: {activity.get('type')}")


async def _process_inbox_follow(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    """The actor is requesting to follow the box owner."""
    box_owner = context.box_owner
    if resource_id(activity.get("object")) != box_owner.get("id"):
        raise InvalidRequestException("Mismatch between object and box owner")
    actor_uri = resource_id(activity.get("actor"))
    if actor_uri == box_owner.get("id"):
        raise InvalidRequestException("Cannot follow self")
    collection_uri = box_owner.get("followers")
    if not collection_uri:
        raise NotImplementedError("Following not supported. No followers collection")
    store = context.tenant.public_store
    await add_collection_item(store, collection_uri, resource_id(actor_uri))
    # TODO Make auto-accept configurable
    # TODO need a way to identify pending follow requests in store
    log.info(f"Sending Accept to {actor_uri}")
    await context.send(
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": generate_id(box_owner, "accept"),
            "type": "Accept",
            "actor": box_owner.get("id"),
            "to": actor_uri,
            "object": activity,
        }
    )


async def _process_inbox_accept(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    """A remote actor has accepted our follow request."""
    accepted_activity_uri = resource_id(activity.get("object"))
    accepted_activity = await context.dereference(accepted_activity_uri)
    if accepted_activity and accepted_activity_uri == resource_id(accepted_activity):
        if not is_type(accepted_activity, "Follow"):
            raise InvalidRequestException("Accepting non-Follow object")
        following_uri = context.box_owner.get("following")
        if not following_uri:
            raise NotImplementedError("Following not supported")
        store = context.tenant.public_store
        await add_collection_item(store, following_uri, resource_id(accepted_activity["object"]))
    else:
        raise InvalidResourceException("Unknown accepted object")


async def _process_inbox_reject(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    """A remote actor has rejected our follow request."""
    rejected_activity_uri = resource_id(activity.get("object"))
    if rejected_activity := await context.dereference(rejected_activity_uri):
        if not is_type(rejected_activity, "Follow"):
            raise InvalidRequestException("Accepting non-Follow object")
        following_uri = context.box_owner.get("following")
        if not following_uri:
            raise NotImplementedError("Following not supported")
        store = context.tenant.public_store
        await remove_collection_item(store, following_uri, resource_id(rejected_activity["object"]))
    else:
        raise InvalidResourceException("Unknown accepted object")


async def _process_inbox_like(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    liked_object_uri = resource_id(activity.get("object"))
    if liked_object := await context.dereference(liked_object_uri, required=True):
        collection_uri = resource_id(liked_object["likes"])
        store = context.tenant.public_store
        await add_collection_item(store, collection_uri, resource_id(activity.get("actor")))
    else:
        raise InvalidResourceException("Unknown liked object")


async def _process_inbox_create(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    activity_object = activity["object"]
    if isinstance(activity_object, Mapping):
        activity["object"] = resource_id(activity_object)
        await context.keep(activity_object)
        await context.keep(activity)


async def _process_inbox_delete(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    """Another server has withdrawn a document: drop whatever copy we cached.

    The object is either the deleted URI or an embedded ``Tombstone`` naming it.
    """
    target = get_id(activity.get("object"))
    if target is None:
        raise InvalidRequestException("Delete without an object")
    await context.resolver.invalidate(str(target))


async def _process_inbox_undo(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    # TODO If only URI retrieve remote object
    if resource_get(activity, "object", "type") == "Follow":
        return await _process_undo_follow(context, activity)
    elif resource_get(activity, "object", "type") == "Like":
        await _process_inbox_undo_like(context, activity)
    else:
        raise NotImplementedError("Undo not supported for this object type")


async def _process_undo_follow(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    """A remote actor undoes their follow of the box owner.

    Only the owner's own followers collection is involved: the actor to drop is
    the sender, and the owner document is already loaded, so nothing is fetched.
    """
    follower_uri = resource_id(activity.get("actor"))
    if follower_uri is None:
        raise InvalidRequestException("Request has no actor to remove")
    followers_uri = get_id(context.box_owner.get("followers"))
    if followers_uri is None:
        raise InvalidResourceException("No followers collection")
    await remove_collection_item(context.tenant.public_store, followers_uri, follower_uri)


async def _process_inbox_undo_like(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    liked_object_uri = resource_id(resource_get(activity, "object", "object"))
    if liked_object := await context.dereference(liked_object_uri, required=True):
        if collection_uri := resource_id(liked_object["likes"]):
            store = context.tenant.public_store
            await remove_collection_item(store, collection_uri, resource_id(activity.get("actor")))
            return
    raise InvalidRequestException("Unable to undo like")


async def _process_inbox_undo_announce(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    announced_object_uri = resource_id(resource_get(activity, "object", "object"))
    if announced_object := await context.dereference(announced_object_uri, required=True):
        if collection_uri := resource_id(announced_object.get("shares")):
            store = context.tenant.public_store
            await remove_collection_item(store, collection_uri, resource_id(activity.get("actor")))
    raise InvalidRequestException("Unable to undo announce")


async def _process_inbox_announce(
    context: FirmBoxContext,
    activity: JSONObject,
) -> None:
    if "object" not in activity:
        raise InvalidRequestException("Missing object in announce")
    announced_object_uri = resource_id(activity["object"])
    announced_object = await context.dereference(announced_object_uri)
    if announced_object is None:
        raise InvalidRequestException("Unknown announced object")
    if "shares" not in announced_object:
        shares: JSONObject = {
            "id": f"{announced_object['id']}/shares",
            "type": "OrderedCollection",
            "attributedTo": announced_object.get("attributedTo", context.box_owner["id"]),
            "totalItems": 0,
            "items": [],
        }
    else:
        shares_uri = resource_id(announced_object.get("shares"))
        shares = await context.dereference(shares_uri, required=True)
    await add_collection_item(
        context.tenant.public_store, resource_id(shares), resource_id(activity)
    )
