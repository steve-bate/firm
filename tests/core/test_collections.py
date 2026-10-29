"""Collection counts must stay correct on the remove path, not just the add path.

``remove_collection_item`` mutates the member list but never recomputes
``totalItems``, so a client is told the collection still holds the member that
was just removed. Covered on both backends: the filesystem store is what a
deployed tenant uses, the memory store is what most of the suite uses.
"""

import pytest

from firm.core.services.activitypub.collections import remove_collection_item
from firm.core.store.file import FileResourceStore
from firm.core.store.memory import MemoryResourceStore

COLLECTION = "http://tenant1.test/followers"
MEMBER_A = "http://remote.test/a"
MEMBER_B = "http://remote.test/b"


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
            "items": [MEMBER_B, MEMBER_A],
            "totalItems": 2,
        }
    )

    await remove_collection_item(store, COLLECTION, MEMBER_B)

    collection = await store.get(COLLECTION)
    assert collection["items"] == [MEMBER_A]
    assert collection["totalItems"] == 1
