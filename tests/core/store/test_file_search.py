"""The filesystem store searches its own partition, one document at a time."""

import pytest

from firm.core.store.file import FileResourceStore


@pytest.fixture
def store(tmp_path) -> FileResourceStore:
    return FileResourceStore(tmp_path)


async def _seed(store: FileResourceStore) -> None:
    await store.put({"id": "https://example.test/notes/1", "summary": "a note about cats"})
    await store.put({"id": "https://example.test/notes/2", "summary": "a note about dogs"})
    await store.put({"id": "https://example.test/notes/3", "content": "cats again, at length"})


async def test_it_matches_documents_in_its_partition(store):
    await _seed(store)

    page = await store.search("cats")

    assert sorted(d["id"] for d in page.documents) == [
        "https://example.test/notes/1",
        "https://example.test/notes/3",
    ]
    assert page.total == 2


async def test_the_window_is_stable_and_counts_every_match(store):
    await _seed(store)

    first = await store.search("cats", limit=1)
    second = await store.search("cats", start=1, limit=1)
    again = await store.search("cats", limit=1)

    assert [d["id"] for d in first.documents] == [d["id"] for d in again.documents]
    assert first.total == 2
    assert [d["id"] for d in second.documents] != [d["id"] for d in first.documents]
    assert second.start_index == 1


async def test_a_search_does_not_leave_the_partition(tmp_path):
    public = FileResourceStore(tmp_path, "public")
    private = FileResourceStore(tmp_path, "private")
    await public.put({"id": "https://example.test/notes/1", "summary": "cats are public"})
    await private.put({"id": "urn:private:1", "summary": "cats are private"})

    page = await public.search("cats")

    assert [d["id"] for d in page.documents] == ["https://example.test/notes/1"]
    assert page.total == 1


async def test_the_text_fields_are_the_ones_asked_for(store):
    await _seed(store)

    content_only = await store.search("cats", text_fields=["content"])

    assert [d["id"] for d in content_only.documents] == ["https://example.test/notes/3"]
