from typing import Any

import pytest

from firm.interfaces import JSONObject
from firm.store.sqlite import SqliteResourceStore


@pytest.fixture
def partition(tmp_path):
    db_filepath = str(tmp_path / "objects.sqlite")
    partition = SqliteResourceStore("default", db_filepath)
    yield partition
    partition.close()


async def test_put_get_remove(partition):
    id_ = "http://server.test/obj1"
    original_obj = {"id": id_, "type": "Something", "name": "foo"}
    await partition.put(original_obj)
    modified_obj = {"id": id_, "type": "Something", "summary": "bar"}
    await partition.put(modified_obj)
    # Be sure data was flushed
    partition.commit()
    assert await partition.is_stored(id_)
    assert not await partition.is_stored("BOGUS")
    stored_obj = await partition.get(id_)
    # complete replacement
    assert stored_obj == modified_obj
    await partition.remove(id_)
    assert (await partition.get(id_)) is None


async def test_query_single_value(partition):
    objects = [
        {
            "id": f"http://server.test/obj-{i}",
            "name": f"Thing-{i}",
            "type": "Something",
        }
        for i in range(5)
    ]
    for obj in objects:
        await partition.put(obj)
    query_results = await partition.query({"name": "Thing-3"})
    assert sorted(r["id"] for r in query_results) == ["http://server.test/obj-3"]
    assert (await partition.query({"name": "Thing-999"})) == []


async def test_query_list_value(partition) -> None:
    objects: list[dict[str, Any]] = [
        {
            "id": "http://server.test/obj-0",
            "name": "Thing",
            "type": ["Something", "SomethingElse"],
        },
        {
            "id": "http://server.test/obj-1",
            "name": "Thing",
            "type": ["SomethingElse"],
        },
        {
            "id": "http://server.test/obj-2",
            "name": "Thing",
            "summary": "test",
            "type": ["Something"],
        },
        {
            "id": "http://server.test/obj-3",
            "name": "Thing",
            "summary": "test",
            "type": "Something",
        },
    ]
    for obj in objects:
        await partition.put(obj)
    query_results = await partition.query({"type": "Something"})
    assert sorted(r["id"] for r in query_results) == [
        "http://server.test/obj-0",
        "http://server.test/obj-2",
        "http://server.test/obj-3",
    ]


async def test_query_multikey(partition) -> None:
    objects: list[JSONObject] = [
        {
            "id": "http://server.test/obj-0",
            "name": "Thing",
            "type": ["Something", "SomethingElse"],
        },
        {
            "id": "http://server.test/obj-1",
            "name": "Thing",
            "type": ["SomethingElse"],
        },
        {
            "id": "http://server.test/obj-2",
            "name": "Thing",
            "summary": "test",
            "type": ["Something"],
        },
        {
            "id": "http://server.test/obj-3",
            "name": "Thing",
            "summary": "test",
            "type": "Something",
        },
    ]
    for obj in objects:
        await partition.put(obj)
    query_results = await partition.query({"type": "Something", "summary": "test"})
    assert sorted(r["id"] for r in query_results) == [
        "http://server.test/obj-2",
        "http://server.test/obj-3",
    ]
