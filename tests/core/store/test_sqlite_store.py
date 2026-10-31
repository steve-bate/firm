from typing import Any

import pytest

from firm.core.interfaces import JSONObject
from firm.core.store.sqlite import SqliteResourceStore


@pytest.fixture
async def partition(tmp_path):
    db_filepath = str(tmp_path / "objects.sqlite")
    partition = SqliteResourceStore("default", db_filepath)
    yield partition
    await partition.close()


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


async def test_query_one_returns_the_match(partition):
    await partition.put({"id": "http://server.test/obj1", "name": "foo"})

    found = await partition.query_one({"name": "foo"})

    assert found is not None and found["id"] == "http://server.test/obj1"
    assert await partition.query_one({"name": "absent"}) is None


async def test_update_changes_the_stored_object(partition):
    await partition.put({"id": "http://server.test/obj1", "name": "foo"})

    await partition.update("http://server.test/obj1", {"name": "bar"})

    assert (await partition.get("http://server.test/obj1"))["name"] == "bar"


async def test_upsert_creates_then_updates(partition):
    await partition.upsert({"id": "http://server.test/obj1"}, {"name": "foo"})
    await partition.upsert({"id": "http://server.test/obj1"}, {"name": "bar"})

    assert (await partition.get("http://server.test/obj1"))["name"] == "bar"


async def test_query_binds_its_values(partition):
    await partition.put({"id": "http://server.test/obj1", "name": "it's a name"})

    found = await partition.query({"name": "it's a name"})

    assert [r["id"] for r in found] == ["http://server.test/obj1"], "not a syntax error"
    assert await partition.query({"name": "x' or '1'='1"}) == [], "not an injection"


async def test_query_with_no_criteria_returns_the_partition(partition):
    await partition.put({"id": "http://server.test/obj1"})
    await partition.put({"id": "http://server.test/obj2"})

    assert len(await partition.query({})) == 2


async def test_put_replaces_rather_than_duplicating(partition):
    for summary in ("one", "two"):
        await partition.put({"id": "http://server.test/obj1", "summary": summary})

    (rows,) = partition.connection.execute("SELECT COUNT(*) FROM objects").fetchone()

    assert rows == 1, "replace in place, one row"
