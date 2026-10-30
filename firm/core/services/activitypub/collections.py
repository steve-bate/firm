import logging
from typing import TYPE_CHECKING, cast

from firm.core.interfaces import JSONObject, ResourceStore
from firm.core.util import dereference, is_local_uri, is_public

if TYPE_CHECKING:
    from firm.core.services.activitypub.support import FirmContext

log = logging.getLogger(__name__)


async def add_collection_item(
    store: ResourceStore,
    collection_uri: str,
    item_uri: str,
    prepend=True,
    allow_dups=False,
):
    collection = await dereference(store, collection_uri)
    if not collection:
        raise ValueError(f"Unknown collection: {collection_uri}")
    # For storage, only 'items' is used
    # Serialization will serialized as orderedItems if needed
    items_key = "items"
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


async def remove_collection_item(store: ResourceStore, collection_uri: str, item_uri: str):
    collection = await dereference(store, collection_uri)
    if not collection:
        raise ValueError(f"Unknown collection: {collection_uri}")
    # For storage, only 'items' is used
    # Serialization will serialized as orderedItems if needed
    items_key = "items"
    items = collection.get(items_key)
    if isinstance(items, list):
        if item_uri in items:
            items.remove(item_uri)
        collection["totalItems"] = len(items)
    await store.put(collection)


async def dereference_collection_items(
    context: FirmContext, collection: JSONObject, inplace=False
) -> list[JSONObject] | JSONObject | None:
    # all stored collections use "items" for storage,
    # even if they serialize as "orderedItems"
    items_key = "items"
    items = collection.get(items_key, [])
    if isinstance(items, list):
        dereferenced_items: list[JSONObject] = []
        for item in items:
            if isinstance(item, str):
                resolved = await context.dereference(item)
                if resolved is None:
                    log.warning("Not serving unresolvable collection item: %s", item)
                    continue
                if not is_local_uri(context.tenant, item) and not is_public(resolved):
                    log.info("Not serving non-public remote document: %s", item)
                    continue
                dereferenced_items.append(resolved)
            else:
                dereferenced_items.append(item)
        if inplace:
            # TODO Review the collection item dereferencing and clean it up
            collection[items_key] = dereferenced_items
            return collection
        else:
            return dereferenced_items
    raise Exception("Collection items must be a list")
