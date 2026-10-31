import logging
from typing import TYPE_CHECKING, cast

from firm.core.interfaces import JSONObject, ResourceStore
from firm.core.services.activitypub.exceptions import InvalidResourceException

if TYPE_CHECKING:
    from firm.core.services.activitypub.support import FirmContext

log = logging.getLogger(__name__)


async def add_collection_item(store: ResourceStore, collection_uri: str, item_uri: str) -> int:
    """Add item_uri to collection_uri as its newest item; return the new count.

    The item is added once: the store appends whatever it is given, duplicates included —
    AS2 puts no uniqueness rule on collection items — so a caller that wants a set asks the
    store whether it already holds the item, which is what every current caller wants.
    Raises ``UnknownCollectionError`` for a collection that does not exist.
    """
    if await store.contains_item(collection_uri, item_uri):
        return await store.count_items(collection_uri)
    return await store.append_item(collection_uri, item_uri)


async def remove_collection_item(store: ResourceStore, collection_uri: str, item_uri: str) -> int:
    """Remove item_uri from collection_uri if the collection holds it; return the count."""
    return await store.remove_item(collection_uri, item_uri)


RELATIONSHIP_COLLECTIONS = frozenset({"followers", "following", "liked"})


def embeds_items(collection: JSONObject) -> bool:
    """Whether a collection is served with its items embedded.

    A relationship collection is a list of actors this server does not hold, so it
    is served as IRIs: embedding would fetch every one of them on every read and
    silently drop whoever cannot be fetched.
    """
    uri = collection.get("id")
    if not isinstance(uri, str):
        return True
    return uri.rstrip("/").rsplit("/", 1)[-1] not in RELATIONSHIP_COLLECTIONS


async def dereference_collection_items(
    context: FirmContext,
    collection: JSONObject,
    *,
    embed: bool = True,
    inplace: bool = False,
) -> list[JSONObject | str] | JSONObject | None:
    # all stored collections use "items" for storage,
    # even if they serialize as "orderedItems"
    items_key = "items"
    items = collection.get(items_key, [])
    if isinstance(items, list):
        if not embed:
            return collection if inplace else cast(list[JSONObject | str], list(items))
        dereferenced_items: list[JSONObject | str] = []
        for item in items:
            if isinstance(item, str):
                resolved = await context.dereference(item)
                if resolved is None:
                    log.warning("Not serving unresolvable collection item: %s", item)
                    continue
                decision = await context.authorizer.is_get_authorized(
                    context.tenant, context.principal, resolved
                )
                if not decision.authorized:
                    log.info(
                        "Not serving unauthorized collection item: %s (%s)",
                        item,
                        decision.reason,
                    )
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
    raise InvalidResourceException("Collection items must be a list")
