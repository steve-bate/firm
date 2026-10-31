"""The pieces of a collection page that need no store: options, links, the document."""

import pytest

from firm.core.interfaces import (
    DEFAULT_COLLECTION_PAGE_SIZE,
    MAX_COLLECTION_PAGE_SIZE,
    CollectionPage,
)
from firm.core.services.activitypub.collections import (
    LAST,
    build_page_document,
    option_value,
    page_number,
    page_size,
    page_uri,
)

COLLECTION = "http://tenant1.test/user1/outbox"


@pytest.mark.parametrize(
    "options, expected",
    [
        (None, None),
        ({}, None),
        ({"limit": "5"}, "5"),
        ({"limit": ["5"]}, "5"),
        ({"limit": ["5", "9"]}, "5"),
        ({"limit": []}, None),
    ],
)
def test_option_value_accepts_what_the_framework_hands_over(options, expected):
    assert option_value(options, "limit") == expected


@pytest.mark.parametrize(
    "options, expected",
    [
        (None, DEFAULT_COLLECTION_PAGE_SIZE),
        ({"limit": "5"}, 5),
        ({"limit": "10000"}, MAX_COLLECTION_PAGE_SIZE),
    ],
)
def test_page_size(options, expected):
    assert page_size(options) == expected


def test_page_size_below_one_raises():
    with pytest.raises(ValueError):
        page_size({"limit": "0"})


def test_page_size_rejects_a_non_number():
    with pytest.raises(ValueError):
        page_size({"limit": "plenty"})


@pytest.mark.parametrize(
    "options, expected",
    [
        (None, None),
        ({}, None),
        ({"page": "true"}, 1),
        ({"page": ["true"]}, 1),
        ({"page": "3"}, 3),
        ({"page": ["3"]}, 3),
        ({"page": "last"}, LAST),
        ({"page": "nonsense"}, None),
        ({"page": "0"}, None),
    ],
)
def test_page_number(options, expected):
    assert page_number(options) == expected


def test_page_uri_omits_the_default_limit():
    assert page_uri(COLLECTION, page="true") == f"{COLLECTION}?page=true"


def test_page_uri_carries_a_non_default_limit():
    assert (
        page_uri(COLLECTION, cursor="start:20", limit=5)
        == f"{COLLECTION}?cursor=start%3A20&limit=5"
    )


def test_page_uri_drops_an_existing_query_and_fragment():
    assert page_uri(f"{COLLECTION}?page=3#frag", page="last") == f"{COLLECTION}?page=last"


def test_build_page_document_carries_the_neighbours():
    page = CollectionPage(
        items=("http://tenant1.test/note/3",),
        cursor="cursor-next",
        prev_cursor="start:0",
        total=30,
        start_index=2,
    )

    document = build_page_document(
        {"id": COLLECTION, "type": "OrderedCollection"},
        page,
        request_uri=f"{COLLECTION}?page=2",
        limit=2,
    )

    assert document["id"] == f"{COLLECTION}?page=2"
    assert document["type"] == "OrderedCollectionPage"
    assert document["partOf"] == COLLECTION
    assert document["totalItems"] == 30
    assert document["startIndex"] == 2
    assert document["items"] == ["http://tenant1.test/note/3"]
    # The links carry the page size, so following one keeps the window the same size.
    assert document["next"] == f"{COLLECTION}?cursor=cursor-next&limit=2"
    assert document["prev"] == f"{COLLECTION}?cursor=start%3A0&limit=2"


def test_build_page_document_omits_a_missing_neighbour():
    document = build_page_document(
        {"id": COLLECTION, "type": "Collection"},
        CollectionPage(items=("http://tenant1.test/note/1",), total=1),
        request_uri=COLLECTION,
        limit=20,
    )

    assert document["type"] == "CollectionPage"
    assert "next" not in document
    assert "prev" not in document
