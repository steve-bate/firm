import logging
from datetime import datetime
from typing import Any, Mapping, cast

from jsonpointer import JsonPointer

from firm.core.interfaces import FIRM_NS, JSONObject
from firm.core.services.activitypub.collections import (
    add_collection_item,
    ensure_object_collection,
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
    AS2_ACTOR_TYPES,
    AS2_CONTEXT,
    get_id,
    get_list,
    get_types,
    has_value,
    is_local_uri,
    is_type,
    is_type_any,
    resource_get,
    resource_id,
)

log = logging.getLogger(__name__)

# The AS2 activity types: AP 6.2.1 reaches only non-activities.
AS2_ACTIVITY_TYPES = frozenset(
    {
        "Accept",
        "Activity",
        "Add",
        "Announce",
        "Arrive",
        "Block",
        "Create",
        "Delete",
        "Disagree",
        "Flag",
        "Follow",
        "Ignore",
        "IntransitiveActivity",
        "Invite",
        "Join",
        "Leave",
        "Like",
        "Listen",
        "Move",
        "Offer",
        "Question",
        "Read",
        "Reject",
        "Remove",
        "TentativeAccept",
        "TentativeReject",
        "Travel",
        "Undo",
        "Update",
        "View",
    }
)

# A link, mention or hashtag is addressed by another property; a collection is not
# content a Create can introduce.
AS2_REFERENCE_TYPES = frozenset(
    {
        "Collection",
        "CollectionPage",
        "Hashtag",
        "Link",
        "Mention",
        "OrderedCollection",
        "OrderedCollectionPage",
    }
)

# A document naming one of these is not introduced by a Create: actors are
# provisioned rather than created through an outbox, and `Patch` is firm's own
# activity type. A type the AS2 vocabulary does not define is the caller's own
# object type, and is wrapped.
NON_OBJECT_TYPES = AS2_ACTIVITY_TYPES | AS2_ACTOR_TYPES | AS2_REFERENCE_TYPES | frozenset({"Patch"})


def _introduces_an_object(document: JSONObject) -> bool:
    """Whether the outbox POST is an object, which AP 6.2.1 has a Create introduce."""
    types = get_types(document)
    return bool(types) and not any(t in NON_OBJECT_TYPES for t in types)


def _as_create(document: JSONObject) -> JSONObject:
    """The Create that introduces a posted object, carrying the object's audiences.

    AP 6.2.1 requires the properties to be copied over; a recipient named in two
    of them stays in both, as the sender wrote it.
    """
    create: JSONObject = {"@context": AS2_CONTEXT, "type": "Create", "object": document}
    for key in ("to", "bto", "cc", "bcc", "audience"):
        if key in document:
            create[key] = document[key]
    return create


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


async def process_outbox(context: FirmBoxContext, activity: JSONObject) -> str | None:
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
    posted_object = _introduces_an_object(activity)
    if posted_object:
        activity = _as_create(activity)
    actor_uri = resource_id(context.box_owner)
    activity_id = generate_id(actor_uri, activity)
    activity["id"] = activity_id
    # Fill in missing fields
    if "actor" not in activity:
        activity["actor"] = actor_uri
    if "attributedTo" not in activity:
        activity["attributedTo"] = actor_uri
    if has_value(activity, "type", "Create"):
        if posted_object:
            await _store_create(context, activity)
        else:
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
            elif has_value(activity, "type", "Follow"):
                await _process_outbox_follow(context, activity)
            elif has_value(activity, "type", "Undo"):
                await _process_outbox_undo(context, activity)
            elif has_value(activity, "type", "Patch"):
                await _process_outbox_patch(context, activity)
            else:
                # raise NotImplementedError(f"Unsupported activity type: {activity.get('type')}")
                ...
        finally:
            if isinstance(activity.get("object"), Mapping) and (
                object_uri := get_id(activity.get("object"))
            ):
                # The handler owns what the activity carries: it stored the payload when
                # it accepted it (a Delete stores only the Tombstone). What is kept here
                # is the activity, so its object is a reference to the payload.
                activity["object"] = object_uri
            await context.keep(activity)
    await context.send(activity)
    return activity_id


async def _save_patched(
    context: FirmContext, target: JSONObject, pointers: list[JsonPointer]
) -> None:
    """Write back the document each patched path belongs to.

    A path may address a value inside a document embedded in the target (an activity's
    ``object``). That value belongs to the embedded document, which has an address of its
    own, so it is that document that is written: saving the whole target would leave the
    embedded copy as a reference and the change nowhere. A path that ends in the target
    itself writes the target.
    """
    written: list[int] = []
    for ptr in pointers:
        try:
            parent, _ = ptr.to_last(target)
        except (KeyError, IndexError, TypeError):
            parent = None
        document = parent if isinstance(parent, Mapping) and get_id(parent) else target
        if id(document) in written:
            continue
        written.append(id(document))
        await context.replace(cast(JSONObject, document))


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
        if op == "add":
            if "value" not in operation:
                raise InvalidRequestException("Add operation missing value in Patch activity")
            ptr.set(target, operation["value"], inplace=True)
            await _save_patched(context, target, [ptr])
        elif op == "remove":
            parent, token = ptr.to_last(target)
            del parent[token]
            await _save_patched(context, target, [ptr])
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
            await _save_patched(context, target, [ptr])
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
            await _save_patched(context, target, [from_ptr, path_ptr])
        elif op == "copy":
            if "from" not in operation:
                raise InvalidRequestException("Move operation missing from in Patch activity")
            from_ptr = JsonPointer(operation["from"])
            value = from_ptr.get(target)
            path_ptr = JsonPointer(operation["path"])
            path_ptr.set(target, value, inplace=True)
            await _save_patched(context, target, [path_ptr])
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
    undone = await _undone_activity(context, activity)
    if undone is None:
        raise InvalidRequestException("Request has no activity to undo")
    if has_value(undone, "type", "Follow"):
        await _process_outbox_undo_follow(context, undone)
    # TODO Implement outbox undo like
    # elif resource_get(activity, "object", "type") == "Like":
    #     await self._process_inbox_undo_like(request, activity)
    # TODO Implement outbox undo announce
    # elif resource_get(activity, "object", "type") == "Announce":
    #     await self._process_inbox_undo_announce(request, activity)


async def _undone_activity(context: FirmBoxContext, activity: JSONObject) -> JSONObject | None:
    """The activity an Undo undoes, from the Undo itself or from our own record.

    An Undo may embed the activity it undoes or name it by IRI. A named one is our
    own activity, so it is read from this tenant's store - never fetched.
    """
    undone = activity.get("object")
    if isinstance(undone, dict):
        return undone
    if isinstance(undone, str):
        return await context.tenant.public_store.get(undone)
    return None


async def _process_outbox_follow(context: FirmBoxContext, activity: JSONObject) -> None:
    """Our follow of an actor enters `following` as the Follow is accepted, not later.

    Mastodon does the same: the row exists from the moment the Follow leaves, and a
    `Reject` or an `Undo` removes it again. The alternative - add the actor only when
    the remote server answers with `Accept` - leaves the relationship in a state no
    document records, so a C2S client cannot tell "requested" from "not following"
    for as long as the answer takes (seconds on a good day, never on a bad one).

    FEP-4ccd (`pendingFollowing`) exists for exactly that interval. If it is ever
    implemented here, this handler is what changes: the Follow joins the pending
    collection instead of `following`, and `Accept` moves it across. See
    `docs/following.md`.
    """
    followed = activity.get("object")
    if followed is None:
        raise InvalidRequestException("Follow has no object")
    followed_uri = resource_id(followed)
    following_uri = get_id(context.box_owner.get("following"))
    if following_uri is None:
        raise InvalidRequestException("Actor has no following collection")
    await add_collection_item(context.tenant.public_store, following_uri, followed_uri)


async def _process_outbox_undo_follow(context: FirmBoxContext, follow: JSONObject) -> None:
    """Undoing our follow of an actor drops that actor from our following.

    The followed actor is named by the Follow we already hold, so the unfollow
    needs no document from the other server.
    """
    followed_uri = resource_id(resource_get(follow, "object"))
    following_uri = get_id(context.box_owner.get("following"))
    if followed_uri is None or following_uri is None:
        raise InvalidRequestException("Request has no follow to undo")
    await remove_collection_item(context.tenant.public_store, following_uri, followed_uri)


async def _process_outbox_like(context: FirmBoxContext, activity: JSONObject) -> None:
    """Record the Like where it can be recorded: on the object's likes and the actor's.

    The liked object is resolved like any other resource - this tenant's store when the
    URI is its own, the resolver when it is not - because a post the actor only read
    through the remote cache is still a post the actor can like. Only a collection this
    server owns is written: a remote object's ``likes`` names a collection on the server
    that object came from, and keeping it here would shadow a document that is not ours.
    """
    store = context.tenant.public_store
    liked_object_uri = resource_id(activity.get("object"))
    liked_object = await context.dereference(liked_object_uri)
    if liked_object is None:
        raise InvalidRequestException("Unknown liked object")
    likes_collection_uri = await ensure_object_collection(context, liked_object, "likes")
    if likes_collection_uri is not None:
        await add_collection_item(store, likes_collection_uri, resource_id(activity.get("actor")))
    # Get actor's liked collection and add the liked object to it
    actor = await store.get(str(activity["actor"]))
    if actor and "liked" in actor:
        liked_collection_uri = resource_id(actor.get("liked"))
        if liked_collection_uri:
            await add_collection_item(store, liked_collection_uri, liked_object_uri)


async def _process_outbox_remove(context: FirmBoxContext, activity: JSONObject) -> None:
    target = activity.get("target")
    if isinstance(target, str):
        target = await context.dereference(target)
    if not target or not isinstance(target, Mapping):
        raise InvalidResourceException("Invalid target collection")
    if "id" not in target:
        raise InvalidResourceException("Target collection has no ID")
    if "object" not in activity:
        raise InvalidRequestException("Missing object in Remove")
    object_ = activity["object"]
    if isinstance(object_, str):
        object_ = await context.dereference(object_)
    if not object_ or not isinstance(object_, Mapping):
        raise InvalidRequestException("Invalid object to remove")
    if "id" not in object_:
        raise InvalidResourceException("Object has no ID")
    store = context.tenant.public_store
    # Only the store knows what the collection holds: removing an item it does not hold is
    # a no-op, not an error.
    await store.remove_item(resource_id(target), resource_id(object_))
    activity["target"] = resource_id(target)
    activity["object"] = resource_id(object_)
    # Save the activity
    await context.keep(activity)


async def _process_outbox_add(context: FirmBoxContext, activity: JSONObject) -> None:
    target = activity.get("target")
    if isinstance(target, str):
        target = await context.dereference(target)
    if not target or not isinstance(target, Mapping):
        raise InvalidResourceException("Invalid target collection")
    if "id" not in target:
        raise InvalidRequestException("Target collection has no ID")
    if "items" not in target:
        if not is_type_any(target, ("Collection", "OrderedCollection")):
            raise InvalidResourceException("Target collection has no items property")
    if "object" not in activity:
        raise InvalidRequestException("Missing object in Add")
    object_ = activity["object"]
    if isinstance(object_, str):
        object_ = await context.dereference(object_)
    if not object_ or not isinstance(object_, Mapping):
        raise InvalidRequestException("Invalid object to add")
    if "id" not in object_:
        raise InvalidResourceException("Object has no ID")
    if "attributedTo" not in object_:
        object_["attributedTo"] = activity.get("actor", context.tenant.prefix)
    store = context.tenant.public_store
    await context.keep(object_)
    # The outbox is newest first.
    await store.insert_item(resource_id(target), resource_id(object_))
    activity["target"] = resource_id(target)
    activity["object"] = resource_id(object_)
    # Save the activity
    await context.keep(activity)


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
            # An Update replaces the document it names; `keep` only ever references one.
            await context.replace(object_)
            await _publish_attachments(context, object_)
            activity["object"] = resource_id(object_)
            await context.keep(activity)


async def _process_outbox_delete(context, activity):
    if resource := await context.dereference(cast(str, resource_id(activity["object"]))):
        store = context.tenant.public_store
        await context.replace(
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
    await _store_create(context, activity)


# The fields an audience is read from and written to. `bto`/`bcc` are deliberately absent:
# a media object's recipient list is visible to everyone who may read that object, so
# copying a blind copy onto it would tell the other recipients who was hiding.
_AUDIENCE_FIELDS = ("to", "cc", "audience")


def _attachment_uris(document: JSONObject) -> list[str]:
    """Every attachment the document names, as a URI — an inline node or a reference alike."""
    value = document.get("attachment")
    if value is None:
        return []
    entries = value if isinstance(value, list) else [value]
    uris: list[str] = []
    for entry in entries:
        uri = entry if isinstance(entry, str) else get_id(entry)
        if uri:
            uris.append(uri)
    return uris


async def _publish_attachments(context: FirmBoxContext, document: JSONObject) -> None:
    """Give each media object this document attaches the audience the document carries.

    An upload happens before the note that references it exists, so the media object is
    stored with no audience and answers only to its uploader (the authorizer's
    `attributedTo` rule). The note is where an audience is decided, and the bytes are
    served under the same policy as the object that names them — so the note has to hand
    its audience to its attachments, or nobody but the author can fetch them.

    Widening only ever adds. One public reference makes the file public for good, which
    is the safe direction: an attachment a reader has already fetched cannot be
    un-published by deleting the note, and answering 404 for bytes somebody already has
    would only break the readers the post reached.

    This is an explicit write, not a side effect of `keep`'s embedded-node walk: the
    documents named here already exist and belong to the poster, and only their audience
    changes.
    """
    audience = {
        field: get_list(document, field) for field in _AUDIENCE_FIELDS if get_list(document, field)
    }
    if not audience:
        return

    for uri in _attachment_uris(document):
        if not is_local_uri(context.tenant, uri):
            continue
        attached = await context.tenant.public_store.get(uri)
        if attached is None or "url" not in attached:
            # Only a file-backed object has bytes to serve; a mention or a link has none.
            continue
        changed = False
        for field, uris in audience.items():
            present = get_list(attached, field)
            missing = [entry for entry in uris if entry not in present]
            if missing:
                attached[field] = present + missing
                changed = True
        if changed:
            await context.tenant.public_store.put(attached)


async def _store_create(context: FirmBoxContext, activity: JSONObject) -> None:
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
        await context.keep(object_)
        await _publish_attachments(context, object_)
        activity["object"] = resource_id(object_)
        await context.keep(activity)
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
                await context.keep(collection_index)
                await context.replace(cast(JSONObject, context.box_owner))
            else:
                collection_index = await context.dereference(collection_index_uri, required=True)
            await add_collection_item(store, resource_id(collection_index), resource_id(object_))
    # # TODO Process activity
    # await self._delivery_service.deliver(tenant, tenants, activity)
    # return activity_id
