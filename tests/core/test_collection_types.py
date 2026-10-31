"""The vocabulary the collection methods are defined in terms of."""

import dataclasses

import pytest

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    MAX_COLLECTION_PAGE_SIZE,
    CollectionPage,
    UnknownCollectionError,
)


def test_a_page_defaults_to_empty_and_unpaged():
    page = CollectionPage()
    assert page.items == ()
    assert page.cursor is None
    assert page.total == 0
    assert page.start_index == 0


def test_a_page_holds_item_uris_newest_first():
    page = CollectionPage(items=("http://server.test/c", "http://server.test/b"), total=2)
    assert page.items == ("http://server.test/c", "http://server.test/b")
    assert page.total == 2


def test_collection_page_is_frozen():
    page = CollectionPage(items=("a",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        page.items = ()  # type: ignore[misc]


def test_an_unknown_collection_error_is_a_value_error():
    """Callers that catch ValueError today keep working."""
    assert issubclass(UnknownCollectionError, ValueError)
    with pytest.raises(ValueError):
        raise UnknownCollectionError("Unknown collection: http://server.test/missing")


def test_the_page_size_constants_bracket_each_other():
    assert DEFAULT_COLLECTION_PAGE_SIZE == 20
    assert MAX_COLLECTION_PAGE_SIZE == 100
    assert DEFAULT_COLLECTION_PAGE_SIZE <= MAX_COLLECTION_PAGE_SIZE
