import copy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, cast

from firm.core.interfaces import FIRM_NS, JSONObject, Url
from firm.core.services.activitypub.collections import (
    dereference_collection_items,
    embeds_items,
)
from firm.core.services.activitypub.exceptions import (
    InvalidResourceException,
    NotFoundException,
)
from firm.core.services.activitypub.support import FirmContext
from firm.core.util import (
    AS2_CONTEXT,
    get_collection_items_key,
    get_list,
    is_collection,
    is_type,
    remove_empty_data,
)


def add_context(resource: JSONObject, context: Url | str) -> None:
    """Make sure ``context`` is one of the document's ``@context`` entries."""
    context = str(context)
    if "@context" not in resource:
        resource["@context"] = context
    elif isinstance(resource["@context"], list):
        if context not in resource["@context"]:
            resource["@context"].append(context)
    elif resource["@context"] != context:
        resource["@context"] = [resource["@context"], context]


@dataclass(frozen=True)
class ReferencePolicy:
    """What a serializer may resolve on the way to handing a document to a reader.

    The store keeps an embedded document as a reference (``support._split_embedded``), so
    every reader - a browser, a peer's server - is handed links unless serialization expands
    them. Which links may be expanded is a policy rather than a detail: serving may ask the
    resolver, delivery must not (``server.delivery._held_document``), and a reader that
    ignores a link - Pixelfed for ``attachment`` and for ``Accept`` - needs it inline.
    """

    #: Resolves one reference. Delivery passes a loader that cannot reach the network.
    load: Callable[[str], Awaitable[JSONObject | None]]
    #: URI prefixes of references this server can answer. ``None`` lets the loader decide,
    #: which is what serving does for an activity's object: a page may hold a peer's Create
    #: whose object the resolver can still fetch.
    object_prefix: str | None = None
    attachment_prefix: str | None = None
    #: ``Create``/``Update`` carry their object; other activities only reference theirs.
    resolve_object: bool = True
    #: Media documents are referenced far more often than they are embedded.
    resolve_attachments: bool = True
    #: Expand ``likes``/``shares`` into collections; delivery carries the URI instead.
    resolve_collections: bool = False
    #: An ``Accept``/``Reject`` names the ``Follow`` it answers, which lives on the
    #: follower's server and is therefore outside ``object_prefix``.
    embed_answer_target: bool = False
    #: Serving one activity fails loudly when its object is gone. A collection item and a
    #: delivery leave the reference instead: the reader can still be handed the activity.
    required_object: bool = False


def _expandable(uri: str, prefix: str | None) -> bool:
    return prefix is None or uri.startswith(prefix)


async def _resolve(uri: str, policy: ReferencePolicy, *, required: bool) -> JSONObject | None:
    found = await policy.load(uri)
    if found is None:
        if required:
            raise NotFoundException(f"Resource {uri} not found")
        return None
    # Deep copy: the caller's document is rewritten around it, and a dereferenced document
    # may be the resolver cache's own.
    return copy.deepcopy(found)


async def _resolve_attachments(document: JSONObject, policy: ReferencePolicy) -> JSONObject:
    """Replace attachment references with the documents they name.

    A reader in the wild reads ``attachment[i].url`` - Pixelfed and Mastodon alike - so a
    note handed a bare IRI arrives with its text and no media at all.
    """
    attachment = document.get("attachment")
    if not attachment or not policy.resolve_attachments:
        return document

    entries = attachment if isinstance(attachment, list) else [attachment]
    resolved: list[Any] = []
    changed = False
    for entry in entries:
        if isinstance(entry, str) and _expandable(entry, policy.attachment_prefix):
            found = await policy.load(entry)
            if found is not None:
                resolved.append(copy.deepcopy(found))
                changed = True
                continue
        resolved.append(entry)

    if changed:
        document["attachment"] = resolved
    return document


async def _resolve_collections(document: JSONObject, policy: ReferencePolicy) -> JSONObject:
    """Expand ``likes``/``shares`` so a client is not asked to fetch a counter."""
    for prop in ("likes", "shares"):
        prop_uri = document.get(prop)
        if not isinstance(prop_uri, str):
            continue
        collection = await policy.load(prop_uri)
        if collection is None:
            continue
        collection = copy.deepcopy(collection)
        collection.pop("items", None)
        collection.pop("attributedTo", None)
        document[prop] = collection
    return document


async def resolve_references(document: JSONObject, policy: ReferencePolicy) -> JSONObject:
    """Replace the references ``policy`` allows with the documents they name.

    The one place a stored reference becomes the thing it points at, for every reader: a
    served activity, a collection item, an outbound delivery. ``document`` is rewritten in
    place and returned - callers pass a copy.
    """
    if policy.embed_answer_target and (is_type(document, "Accept") or is_type(document, "Reject")):
        uri = document.get("object")
        if isinstance(uri, str):
            held = await _resolve(uri, policy, required=False)
            if held is not None:
                document["object"] = held
        return document

    if not (is_type(document, "Create") or is_type(document, "Update")):
        return document

    uri = document.get("object")
    if policy.resolve_object and isinstance(uri, str) and _expandable(uri, policy.object_prefix):
        obj = await _resolve(uri, policy, required=policy.required_object)
        if obj is not None:
            obj = await _resolve_attachments(obj, policy)
            if policy.resolve_collections:
                obj = await _resolve_collections(obj, policy)
            document["object"] = obj
    return document


def _serving_policy(context: FirmContext, *, required_object: bool) -> ReferencePolicy:
    """Serving: the resolver may fetch, media stays local, counters are expanded."""
    return ReferencePolicy(
        load=context.dereference,
        object_prefix=None,
        attachment_prefix=str(context.tenant.prefix),
        resolve_collections=True,
        required_object=required_object,
    )


async def serialize(context: FirmContext, resource: JSONObject) -> JSONObject:
    """Embed specific resources to match typical AP expectations.

    The document is copied before anything is rewritten, so the caller's
    resource - often the object a store just handed us - is never modified.
    """
    resource = copy.deepcopy(resource)

    if not isinstance(resource, dict):
        raise InvalidResourceException("Can only serialize JSON objects")

    tenant = context.tenant
    principal = context.principal

    if "@context" not in resource:
        add_context(resource, AS2_CONTEXT)

    if is_collection(resource):
        embed = embeds_items(resource)
        resource = cast(
            JSONObject,
            await dereference_collection_items(context, resource, embed=embed, inplace=True),
        )
        items_key = get_collection_items_key(resource)
        if items_key != "items":
            # This is a bit hacky, but it allows us to store collections in a consistent way
            resource[items_key] = resource.pop("items")
        items = resource.get(items_key)
        if embed and isinstance(items, list):
            # The same embedding a single Create gets: an item stored with a reference
            # is served with the document, or the reader would be handed a link to
            # something it cannot fetch (a peer's non-public object, for one).
            policy = _serving_policy(context, required_object=False)
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    items[index] = await resolve_references(item, policy)

    elif is_type(resource, "Create") or is_type(resource, "Update"):
        resource = await resolve_references(
            resource, _serving_policy(context, required_object=True)
        )

    elif "endpoints" in resource:
        # TODO This endpoint handling is a bit hacky
        resource["endpoints"] |= tenant.endpoints

    # Serialize internal blocks for authorized users
    if principal:
        private = tenant.private_store
        if blocks := await private.query_one(
            {
                "type": FIRM_NS.Blocks.value,
                "attributedTo": principal.uri,
            }
        ):
            resource["blocks"] = get_list(blocks, FIRM_NS.blockedActor.value)
            resource[FIRM_NS.blockedDomain.value] = get_list(blocks, FIRM_NS.blockedDomain.value)
            resource[FIRM_NS.blockedSubnet.value] = get_list(blocks, FIRM_NS.blockedSubnet.value)
            add_context(resource, "https://purl.archive.org/socialweb/blocked")

    return remove_empty_data(resource)
