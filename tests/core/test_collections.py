"""Collection counts must stay correct on the remove path, not just the add path.

The count lives in the document and the item list does not: on the filesystem store the
list is a log beside the collection, so this asserts through the API rather than by reading
an array. Covered on both backends: the filesystem store is what a deployed tenant uses, the
memory store is what most of the suite uses.
"""

import pytest

from firm.core.services.activitypub.collections import remove_collection_item
from firm.core.store.file import FileResourceStore
from firm.core.store.memory import MemoryResourceStore

COLLECTION = "http://tenant1.test/followers"
ITEM_A = "http://remote.test/a"
ITEM_B = "http://remote.test/b"


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryResourceStore()
    return FileResourceStore(tmp_path, "public")


async def test_remove_recomputes_total_items(store):
    await store.put(
        {
            "id": COLLECTION,
            "type": "OrderedCollection",
            "items": [ITEM_B, ITEM_A],
            "totalItems": 2,
        }
    )

    await remove_collection_item(store, COLLECTION, ITEM_B)

    page = await store.page_items(COLLECTION)
    assert page.items == (ITEM_A,)
    assert page.total == 1
    assert (await store.get(COLLECTION))["totalItems"] == 1
