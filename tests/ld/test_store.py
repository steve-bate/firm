import rdflib

from firm.ld.store import RdfResourceStore


async def test_put_get_remove(tmp_path):
    id_ = "http://server.test/obj1"
    # store = RdfResourceStore("test", {}, rdflib.Graph())
    store = RdfResourceStore(rdflib.Graph())
    original_obj = {
        "id": id_,
        "type": "Note",
        "name": "foo",
    }
    await store.put(original_obj)
    modified_obj = {
        "id": id_,
        "type": "Note",
        "summary": "bar",
    }
    await store.put(modified_obj)
    # Check stored data
    assert await store.is_stored(id_)
    assert not await store.is_stored("BOGUS")
    stored_obj = await store.get(id_)
    assert stored_obj is not None
    # complete replacement
    for key in ["id", "type", "summary"]:
        assert stored_obj[key] == modified_obj[key]
    await store.remove(id_)
    assert (await store.get(id_)) is None


async def test_query(tmp_path):
    # store = RdfResourceStore("test", {}, rdflib.Graph())
    store = RdfResourceStore(rdflib.Graph())
    objects = [
        {
            "id": f"http://server.test/obj-{i}",
            "type": "Note",
            "name": f"Note-{i}",
        }
        for i in range(5)
    ]
    for obj in objects:
        await store.put(obj)
    query_results = await store.query({"name": "Note-3"})
    assert len(query_results) == 1
    assert query_results[0]["id"] == "http://server.test/obj-3"
    assert (await store.query({"name": "Thing-999"})) == []


async def test_query_one(tmp_path):
    # store = RdfResourceStore("test", {}, rdflib.Graph())
    store = RdfResourceStore(rdflib.Graph())
    objects = [
        {
            "id": f"http://server.test/obj-{i}",
            "type": "Note",
            "name": f"Note-{i}",
        }
        for i in range(5)
    ]
    for obj in objects:
        await store.put(obj)
    query_results = await store.query_one({"name": "Note-3"})
    assert query_results["id"] == "http://server.test/obj-3"
    assert (await store.query({"name": "Thing-999"})) == []
