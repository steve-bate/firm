from firm.core.interfaces import JSONObject
from firm.core.store.memory import MemoryResourceStore


async def test_memory_storage() -> None:
    region = MemoryResourceStore()
    resource: JSONObject = {"id": "test", "name": "test data"}
    assert not await region.is_stored("test")
    await region.put(resource)
    assert await region.is_stored("test")
    assert await region.get("test") == resource
    await region.update("test", {"name": "updated"})
    updated = await region.get("test")
    assert updated is not None and updated["name"] == "updated"
    await region.upsert({"id": "test"}, {"name": "upserted"})
    upserted = await region.get("test")
    assert upserted is not None and upserted["name"] == "upserted"
    await region.upsert({"id": "test2"}, {"name": "inserted"})
    assert await region.is_stored("test2")


async def test_get_hands_back_a_document_the_caller_may_modify() -> None:
    region = MemoryResourceStore()
    resource: JSONObject = {"id": "test", "type": "Something", "to": ["as:Public"]}
    await region.put(resource)

    fetched = await region.get("test")
    assert fetched is not None
    fetched["type"] = "Modified"
    fetched["to"] = []

    assert await region.get("test") == {"id": "test", "type": "Something", "to": ["as:Public"]}
