from pathlib import Path

import pytest

from firm.store.file import FileResourceStore


@pytest.mark.parametrize("path_type", [Path, str])
async def test_put_get_remove(path_type, tmp_path):
    if path_type == str:  # noqa
        tmp_path = str(tmp_path)
    region = FileResourceStore(tmp_path, "test")
    id_ = "http://server.test/obj1"
    original_obj = {"id": id_, "type": "Something"}
    await region.put(original_obj)
    stored_obj = await region.get(id_)
    assert stored_obj == original_obj
    await region.remove(id_)
    assert (await region.get(id_)) is None


async def test_create_subdir(tmp_path):
    _ = FileResourceStore(tmp_path, "test")
    assert (tmp_path / "test").exists()


# Not part of the protocol
async def test_query(tmp_path):
    region = FileResourceStore(tmp_path, "test")
    objects = [
        {"id": f"http://server.test/obj-{i}", "name": f"Thing-{i}", "type": "Something"}
        for i in range(5)
    ]
    for obj in objects:
        await region.put(obj)
    query_results = await region.query({"name": "Thing-3"})
    assert len(query_results) == 1
    assert query_results[0]["id"] == "http://server.test/obj-3"
    assert (await region.query({"name": "Thing-999"})) == []
