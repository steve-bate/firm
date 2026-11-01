import logging
from typing import TYPE_CHECKING, Any, Mapping, cast
from urllib.parse import urlencode

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    MAX_COLLECTION_PAGE_SIZE,
    CollectionPage,
    JSONObject,
    ResourceStore,
)
from firm.core.services.activitypub.exceptions import InvalidResourceException
from firm.core.util import get_id, is_ordered_collection, strip_query

if TYPE_CHECKING:
    from firm.core.services.activitypub.support import FirmContext

log = logging.getLogger(__name__)


async def add_collection_item(store: ResourceStore, collection_uri: str, item_uri: str) -> int:
    """Insert item_uri into collection_uri; return the new count.

    The item goes in at ``offset``: 0 (the default) is the front, -1 the end. A relationship
    collection is a set, so its callers leave the default.

    The item is added once: the store inserts whatever it is given, duplicates included —
    AS2 puts no uniqueness rule on collection items — so a caller that wants a set asks the
    store whether it already holds the item, which is what every current caller wants.
    Raises ``UnknownCollectionError`` for a collection that does not exist.
    """
    if await store.contains_item(collection_uri, item_uri):
        return await store.count_items(collection_uri)
    return await store.insert_item(collection_uri, item_uri)


async def remove_collection_item(store: ResourceStore, collection_uri: str, item_uri: str) -> int:
    """Remove every copy of item_uri from collection_uri; return the count."""
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


async def _collection_item_uris(context: FirmContext, collection: JSONObject) -> list[str] | None:
    """The item URIs of a collection the store keeps, or None for one it does not hold.

    Walks pages from the start until the walk is exhausted, which is also what migrates a
    collection whose document still holds an array: the store imports it on first touch, so
    serving a collection is enough to move its list out of the document. A collection no
    tenant store holds - a remote copy, or one being built in memory - is None, and the
    caller reads the document it was handed.
    """
    uri = get_id(collection)
    if not uri:
        return None
    for store in (context.tenant.public_store, context.tenant.private_store):
        if not await store.is_stored(uri):
            continue
        items: list[str] = []
        cursor: str | None = None
        while True:
            page = await store.page_items(uri, cursor)
            items.extend(page.items)
            if page.cursor is None:
                return items
            cursor = page.cursor
    return None


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
    if items_key in collection:
        # The document the caller handed us is the truth for this request: it may have been
        # filtered, or built in memory, and the store's copy would not show that.
        items = collection[items_key]
    else:
        # The store owns the list now: the document no longer carries it.
        items = await _collection_item_uris(context, collection) or []
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


LAST = -1
"""What ``page_number`` returns for ``?page=last``."""


def option_value(options: Mapping[str, Any] | None, key: str) -> str | None:
    """One query-string value, whether the framework hands over a string or a list."""
    if not options:
        return None
    value = options.get(key)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value)


def page_size(options: Mapping[str, Any] | None) -> int:
    """The requested page size, clamped to what the store will serve."""
    raw = option_value(options, "limit")
    if raw is None:
        return DEFAULT_COLLECTION_PAGE_SIZE
    try:
        size = int(raw)
    except ValueError:
        raise ValueError(f"Malformed limit: {raw}") from None
    if size < 1:
        raise ValueError("limit must be at least 1")
    return min(size, MAX_COLLECTION_PAGE_SIZE)


def page_number(options: Mapping[str, Any] | None) -> int | None:
    """The requested page: 1 for ``?page=true``, LAST for ``?page=last``, None if unasked."""
    raw = option_value(options, "page")
    if raw is None:
        return None
    if raw == "true":
        return 1
    if raw == "last":
        return LAST
    try:
        number = int(raw)
    except ValueError:
        return None
    return number if number >= 1 else None


def page_uri(
    uri: str,
    *,
    page: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> str:
    """A link back into the same collection, carrying only the parameters that matter."""
    base = strip_query(uri)
    params: list[tuple[str, str]] = []
    if page is not None:
        params.append(("page", page))
    if cursor is not None:
        params.append(("cursor", cursor))
    if limit is not None and limit != DEFAULT_COLLECTION_PAGE_SIZE:
        params.append(("limit", str(limit)))
    return f"{base}?{urlencode(params)}" if params else base


def build_page_document(
    collection: JSONObject,
    page: CollectionPage,
    *,
    request_uri: str,
    limit: int,
) -> JSONObject:
    """One collection page as AS2: a CollectionPage with links to its neighbours.

    Items stay URIs here; ``serialize`` dereferences them with the same drop rules the
    inline path uses, so a page and a whole collection cannot disagree about what may be
    shown.
    """
    collection_uri = get_id(collection) or strip_query(request_uri)
    document: JSONObject = {
        "id": request_uri,
        "type": "OrderedCollectionPage" if is_ordered_collection(collection) else "CollectionPage",
        "partOf": collection_uri,
        "totalItems": page.total,
        "startIndex": page.start_index,
        "items": list(page.items),
    }
    if page.cursor is not None:
        document["next"] = page_uri(collection_uri, cursor=page.cursor, limit=limit)
    if page.prev_cursor is not None:
        document["prev"] = page_uri(collection_uri, cursor=page.prev_cursor, limit=limit)
    return document
