import pytest

from firm.core.interfaces import JSONObject
from firm.search.transform.json import JsonMatcher


@pytest.mark.parametrize(
    "query, text_fields, expected",
    [
        ("cats", ["summary"], True),
        ("dogs", ["summary"], False),
        ("cats", ["content"], False),
        ("cats", ["content", "summary"], True),
    ],
)
def test_configured_text_fields(query, text_fields, expected):
    doc = {
        "content": "No animals here",
        "summary": "This profile likes cats",
    }

    m = JsonMatcher(query, text_fields=text_fields)

    assert m.is_match(doc) == expected


def test_bool_matches_and_not():
    doc = {
        "content": "cats and dogs",
        "summary": "friendly post",
    }
    m = JsonMatcher("cats AND NOT birds", ["content", "summary"])

    assert m.is_match(doc)


@pytest.mark.parametrize(
    "query, expected",
    [
        ("type:Person", True),
        ("preferredUsername:ev*", True),
        ("preferredUsername:eve", False),
    ],
)
def test_facet_and_wildcard(query: str, expected: bool):
    doc: JSONObject = {
        "type": "Person",
        "preferredUsername": "evan",
        "content": "hello",
        "summary": "",
    }

    assert JsonMatcher(query, ["content", "summary"]).is_match(doc) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        ("/activit(y|ies).*/", True),
        (r"actor:/https:\/\/example\.com\/users\/.*/", True),
    ],
)
def test_regex_unfaceted_and_faceted(query: str, expected: bool):
    doc: JSONObject = {
        "content": "This talks about activitypub",
        "summary": "",
        "actor": "https://example.com/users/alice",
    }

    assert JsonMatcher(query, ["content", "summary"]).is_match(doc) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        ("tag:activitypub", True),
        ("tag:from*", True),
        ("tag:fediverse", False),
    ],
)
def test_tag_matches_hashtag_names_only(query: str, expected: bool):
    doc: JSONObject = {
        "tag": [
            {"type": "Mention", "name": "fediverse"},
            {"type": "Hashtag", "name": "activitypub"},
        ],
        "object": {
            "tag": [
                {"type": "Hashtag", "name": "fromobject"},
            ]
        },
        "content": "",
        "summary": "",
    }

    assert JsonMatcher(query, ["content", "summary"]).is_match(doc) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        ("rank:[2 TO 3]", True),
        ("rank:{2 TO 3}", False),
        ("rank:[3 TO *]", True),
    ],
)
def test_facet_range_inclusive_and_exclusive(query: str, expected: bool):
    doc: JSONObject = {
        "rank": 3,
        "content": "",
        "summary": "",
    }

    assert JsonMatcher(query, ["content", "summary"]).is_match(doc) == expected
