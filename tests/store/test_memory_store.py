from firm.interfaces import JSONObject
from firm.store.memory import MemoryResourceStore


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
