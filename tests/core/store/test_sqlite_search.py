"""The sqlite store answers the same query language by compiling it to SQL."""

import pytest

from firm.core.store.sqlite import SqliteResourceStore


@pytest.fixture
def store(tmp_path) -> SqliteResourceStore:
    return SqliteResourceStore("public", str(tmp_path / "public.sqlite"))


async def _seed(store: SqliteResourceStore) -> None:
    await store.put({"id": "https://example.test/notes/1", "type": "Note", "summary": "cats"})
    await store.put({"id": "https://example.test/notes/2", "type": "Note", "summary": "dogs"})
    await store.put(
        {"id": "https://example.test/notes/3", "type": "Note", "content": "cats again, slowly"}
    )


async def test_a_bare_term_matches_the_text_fields(store):
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


async def test_the_text_fields_are_the_ones_asked_for(store):
    await _seed(store)

    content_only = await store.search("cats", text_fields=["content"])

    assert [d["id"] for d in content_only.documents] == ["https://example.test/notes/3"]


async def test_a_facet_matches_a_scalar_field(store):
    await _seed(store)
    await store.put(
        {"id": "https://example.test/actors/erin", "type": "Person", "preferredUsername": "erin"}
    )

    page = await store.search("type:Person")

    assert [d["id"] for d in page.documents] == ["https://example.test/actors/erin"]


async def test_a_facet_matches_a_type_list(store):
    await _seed(store)
    await store.put(
        {"id": "https://example.test/activities/1", "type": ["Create", "Note"], "summary": "x"}
    )

    page = await store.search("type:Create")

    assert [d["id"] for d in page.documents] == ["https://example.test/activities/1"]


async def test_boolean_terms_combine(store):
    await _seed(store)

    page = await store.search("cats AND slowly")

    assert [d["id"] for d in page.documents] == ["https://example.test/notes/3"]


async def test_a_regex_is_matched_by_the_connection(store):
    await _seed(store)

    page = await store.search("/ca[a-z]+s/")

    assert [d["id"] for d in page.documents] == [
        "https://example.test/notes/1",
        "https://example.test/notes/3",
    ]


async def test_each_partition_searches_only_its_own_documents(tmp_path):
    public = SqliteResourceStore("public", str(tmp_path / "public.sqlite"))
    private = SqliteResourceStore("private", str(tmp_path / "private.sqlite"))
    await public.put({"id": "https://example.test/notes/1", "summary": "cats in public"})
    await private.put({"id": "urn:private:1", "summary": "cats in private"})

    page = await public.search("cats")

    assert [d["id"] for d in page.documents] == ["https://example.test/notes/1"]
    assert page.total == 1
