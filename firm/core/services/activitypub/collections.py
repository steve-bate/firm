import logging
from typing import cast

from firm.core.interfaces import JSONObject, ResourceStore, Url

log = logging.getLogger(__name__)


# TODO This is temporary
async def _dereference(store: ResourceStore, url: Url | str):
    if isinstance(url, Url):
        url = str(url)
    return await store.get(url)


async def add_collection_item(
    store: ResourceStore,
    collection_uri: str,
    item_uri: str,
    prepend=True,
    allow_dups=False,
):
    collection = await _dereference(store, collection_uri)
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
    collection = await _dereference(store, collection_uri)
    if not collection:
        raise ValueError(f"Unknown collection: {collection_uri}")
    # For storage, only 'items' is used
    # Serialization will serialized as orderedItems if needed
    items_key = "items"
    if items := collection.get(items_key):
        if isinstance(items, list):
            if item_uri in items:
                items.remove(item_uri)
    await store.put(collection)


async def dereference_collection_items(
    store: ResourceStore, collection: JSONObject, inplace=False
) -> list[JSONObject] | JSONObject | None:
    # all stored collections use "items" for storage,
    # even if they serialize as "orderedItems"
    items_key = "items"
    items = collection.get(items_key, [])
    if isinstance(items, list):
        dereferenced_items: list[JSONObject] = []
        for item in items:
            if isinstance(item, str):
                dereferenced_item = await _dereference(store, item)
                if dereferenced_item:
                    dereferenced_items.append(dereferenced_item)
                else:
                    log.warning(f"Unable to dereference collection item: {item}")
            else:
                dereferenced_items.append(item)
        if inplace:
            # TODO Review the collection item dereferencing and clean it up
            collection[items_key] = dereferenced_items
            return collection
        else:
            return dereferenced_items
    raise Exception("Collection items must be a list")
