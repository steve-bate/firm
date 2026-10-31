"""The search every store inherits: match the query against each document in turn."""

import pytest

from firm.core.interfaces import SearchPage
from firm.core.store.memory import MemoryResourceStore


async def _store(*documents: dict) -> MemoryResourceStore:
    store = MemoryResourceStore()
    for document in documents:
        await store.put(document)
    return store


@pytest.fixture
def documents() -> list[dict]:
    return [
        {"id": "https://example.test/notes/1", "summary": "a note about cats"},
        {"id": "https://example.test/notes/2", "summary": "a note about dogs"},
        {"id": "https://example.test/notes/3", "content": "cats again, at length"},
    ]


async def test_a_bare_term_matches_the_text_fields(documents):
    store = await _store(*documents)

    page = await store.search("cats")

    assert [d["id"] for d in page.documents] == [
        "https://example.test/notes/1",
        "https://example.test/notes/3",
    ]
    assert page.total == 2
    assert page.start_index == 0


async def test_a_query_that_matches_nothing_is_an_empty_page(documents):
    store = await _store(*documents)

    page = await store.search("aardvarks")

    assert page == SearchPage(documents=(), total=0, start_index=0)


async def test_the_window_is_the_limit_and_the_start(documents):
    store = await _store(*documents)

    first = await store.search("cats", limit=1)
    second = await store.search("cats", start=1, limit=1)

    assert [d["id"] for d in first.documents] == ["https://example.test/notes/1"]
    assert first.total == 2, "the total counts every match, not the window"
    assert [d["id"] for d in second.documents] == ["https://example.test/notes/3"]
    assert second.start_index == 1


async def test_the_text_fields_are_the_ones_asked_for(documents):
    store = await _store(*documents)

    summary_only = await store.search("cats", text_fields=["summary"])
    content_only = await store.search("cats", text_fields=["content"])

    assert [d["id"] for d in summary_only.documents] == ["https://example.test/notes/1"]
    assert [d["id"] for d in content_only.documents] == ["https://example.test/notes/3"]


async def test_a_facet_query_reaches_the_document_fields(documents):
    store = await _store(
        {"id": "https://example.test/actors/erin", "type": "Person", "preferredUsername": "erin"},
        {"id": "https://example.test/notes/4", "type": "Note", "summary": "cats"},
    )

    page = await store.search("type:Person")

    assert [d["id"] for d in page.documents] == ["https://example.test/actors/erin"]
