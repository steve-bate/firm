import copy
from typing import cast

from firm.core.interfaces import FIRM_NS, JSONObject, Url
from firm.core.services.activitypub.collections import (
    dereference_collection_items,
    embeds_items,
)
from firm.core.services.activitypub.exceptions import InvalidResourceException
from firm.core.services.activitypub.support import FirmContext
from firm.core.util import (
    AS2_CONTEXT,
    get_collection_items_key,
    get_list,
    is_collection,
    is_type,
    remove_empty_data,
)


def _add_context(resource: JSONObject, context: Url | str):
    context = str(context)
    if "@context" not in resource:
        resource["@context"] = context
    elif isinstance(resource["@context"], list):
        if context not in resource["@context"]:
            resource["@context"].append(context)
    elif resource["@context"] != context:
        resource["@context"] = [resource["@context"], context]


async def _embed_object(
    context: FirmContext, document: JSONObject, *, required: bool
) -> JSONObject:
    """Return a Create/Update with its object reference resolved, or the document.

    The store holds references: an activity is saved with ``object`` as a URI and the
    object as its own document. Serving reverses that - for one activity and for every
    item of a collection alike - so a reader is not asked to fetch what it was already
    sent. The copy is deep: a dereferenced document may be the cache's own, and the
    likes/shares rewrite below must not reach it.
    """
    if not (is_type(document, "Create") or is_type(document, "Update")):
        return document
    uri = document.get("object")
    if not isinstance(uri, str):
        return document
    fetched = (
        await context.dereference(uri, required=True)
        if required
        else await context.dereference(uri)
    )
    if fetched is None:
        return document

    document = copy.deepcopy(document)
    obj = copy.deepcopy(fetched)
    document["object"] = obj
    for prop in ("likes", "shares"):
        prop_uri = obj.get(prop)
        if isinstance(prop_uri, str):
            collection = await context.dereference(prop_uri)
            if collection is not None:
                collection = copy.deepcopy(collection)
                collection.pop("items", None)
                collection.pop("attributedTo", None)
                obj[prop] = collection
    return document


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
        _add_context(resource, AS2_CONTEXT)

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
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    items[index] = await _embed_object(context, item, required=False)

    elif is_type(resource, "Create") or is_type(resource, "Update"):
        resource = await _embed_object(context, resource, required=True)

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
            _add_context(resource, "https://purl.archive.org/socialweb/blocked")

    return remove_empty_data(resource)
