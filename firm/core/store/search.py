"""Search answered by reading every document in a store.

Every store can search this way — it is the default ``ResourceStore.search`` — and none
of them should if their layout can do better: sqlite compiles the same query to SQL, and
a backend with an index of its own overrides ``search`` rather than falling back here.
"""

from collections.abc import Sequence

from firm.core.interfaces import (
    DEFAULT_SEARCH_PAGE_SIZE,
    DEFAULT_TEXT_FIELDS,
    JSONObject,
    ResourceStore,
    SearchPage,
)
from firm.search.transform.json import JsonMatcher


async def search(
    store: ResourceStore,
    query: str,
    *,
    text_fields: Sequence[str] = DEFAULT_TEXT_FIELDS,
    start: int = 0,
    limit: int = DEFAULT_SEARCH_PAGE_SIZE,
) -> SearchPage:
    """Match ``query`` against every document the store holds, then take one window."""
    matcher = JsonMatcher(query, text_fields=list(text_fields))
    matched: list[JSONObject] = [doc for doc in await store.query({}) if matcher.is_match(doc)]
    return SearchPage(
        documents=tuple(matched[start : start + limit]),
        total=len(matched),
        start_index=start,
    )
