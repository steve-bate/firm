import copy
from typing import cast

from firm.core.interfaces import FIRM_NS, JSONObject, Url
from firm.core.services.activitypub.collections import dereference_collection_items
from firm.core.services.activitypub.support import FirmContext
from firm.core.util import (
    AS2_CONTEXT,
    get_collection_items_key,
    get_list,
    is_collection,
    is_type,
    remove_empty_data,
    safe_dereference,
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


async def serialize(context: FirmContext, resource: JSONObject) -> JSONObject:
    """Embed specific resources to match typical AP expectations.

    The document is copied before anything is rewritten, so the caller's
    resource - often the object a store just handed us - is never modified.
    """
    resource = copy.deepcopy(resource)

    if not isinstance(resource, dict):
        raise Exception("Can only serialize JSON objects")

    tenant = context.tenant
    store = tenant.public_store
    principal = context.principal

    if "@context" not in resource:
        _add_context(resource, AS2_CONTEXT)

    if is_collection(resource):
        resource = cast(
            JSONObject, await dereference_collection_items(context, resource, inplace=True)
        )
        items_key = get_collection_items_key(resource)
        if items_key != "items":
            # This is a bit hacky, but it allows us to store collections in a consistent way
            resource[items_key] = resource.pop("items")

    elif is_type(resource, "Create") or is_type(resource, "Update"):
        if isinstance(resource.get("object"), str):
            obj = await safe_dereference(store, cast(str, resource["object"]))
            resource["object"] = obj
            for prop in ["likes", "shares"]:
                if prop in obj:
                    collection = await safe_dereference(store, cast(str, obj[prop]))
                    collection.pop("items")
                    collection.pop("attributedTo")
                    obj[prop] = collection

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
