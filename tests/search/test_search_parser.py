# test_query_parser.py

import pytest

from firm.search.parser import parse_query


@pytest.mark.parametrize(
    "query,expected",
    [
        (
            "cats",
            {"type": "term", "facet": None, "value": "cats"},
        ),
        (
            "mastodon search",
            {
                "type": "bool",
                "op": "AND",
                "left": {"type": "term", "facet": None, "value": "mastodon"},
                "right": {"type": "term", "facet": None, "value": "search"},
            },
        ),
        (
            '"activitypub client"',
            {"type": "phrase", "facet": None, "value": "activitypub client"},
        ),
        (
            "/act.*pub/",
            {"type": "regex", "facet": None, "value": "act.*pub"},
        ),
    ],
)
def test_simple_terms_and_phrases(query, expected):
    assert parse_query(query) == expected


@pytest.mark.parametrize(
    "query,expected_op",
    [
        ("cats AND dogs", "AND"),
        ("cats OR dogs", "OR"),
        ("cats AND NOT dogs", "AND"),
    ],
)
def test_boolean_operators_structure(query, expected_op):
    ast = parse_query(query)
    assert ast["type"] == "bool"
    assert ast["op"] == expected_op


def test_boolean_not():
    ast = parse_query("NOT cats")
    assert ast == {
        "type": "not",
        "expr": {"type": "term", "facet": None, "value": "cats"},
    }


def test_implicit_and():
    ast = parse_query("cats dogs birds")
    # ((cats AND dogs) AND birds)
    assert ast["type"] == "bool"
    assert ast["op"] == "AND"
    assert ast["left"]["type"] == "bool"
    assert ast["left"]["op"] == "AND"
    assert ast["left"]["left"]["value"] == "cats"
    assert ast["left"]["right"]["value"] == "dogs"
    assert ast["right"]["value"] == "birds"


def test_grouping_with_parentheses():
    ast = parse_query("(cats OR dogs) AND birds")
    # (cats OR dogs) is left side of top-level AND
    assert ast["type"] == "bool"
    assert ast["op"] == "AND"

    left = ast["left"]
    assert left["type"] == "bool"
    assert left["op"] == "OR"
    assert left["left"]["value"] == "cats"
    assert left["right"]["value"] == "dogs"

    right = ast["right"]
    assert right["type"] == "term"
    assert right["value"] == "birds"


@pytest.mark.parametrize(
    "query,facet,value",
    [
        ("tag:fediverse", "tag", "fediverse"),
        ("language:en", "language", "en"),
        ("type:Note", "type", "Note"),
    ],
)
def test_simple_field_queries(query, facet, value):
    ast = parse_query(query)
    assert ast["type"] == "facet"
    assert ast["facet"] == facet
    assert ast["expr"]["type"] == "term"
    assert ast["expr"]["value"] == value


def test_field_phrase():
    ast = parse_query('content:"activitypub client"')
    assert ast["type"] == "facet"
    assert ast["facet"] == "content"
    inner = ast["expr"]
    assert inner["type"] == "phrase"
    assert inner["value"] == "activitypub client"


def test_faceted_phrase_with_smart_quotes():
    ast = parse_query("type:Create summary:”activity 21”")
    assert ast == {
        "type": "bool",
        "op": "AND",
        "left": {
            "type": "facet",
            "facet": "type",
            "expr": {"type": "term", "facet": None, "value": "Create"},
        },
        "right": {
            "type": "facet",
            "facet": "summary",
            "expr": {"type": "phrase", "facet": None, "value": "activity 21"},
        },
    }


def test_field_regex():
    ast = parse_query("content:/activit(y|ies).*/")
    assert ast["type"] == "facet"
    assert ast["facet"] == "content"
    inner = ast["expr"]
    assert inner["type"] == "regex"
    assert inner["value"] == "activit(y|ies).*"


def test_regex_with_escaped_slash():
    ast = parse_query(r"/https:\/\/example\.com\/users\/alice/")
    assert ast == {
        "type": "regex",
        "facet": None,
        "value": r"https:\/\/example\.com\/users\/alice",
    }


def test_field_regex_with_escaped_slash():
    ast = parse_query(r"actor:/https:\/\/example\.com\/users\/.*/")
    assert ast["type"] == "facet"
    assert ast["facet"] == "actor"
    assert ast["expr"] == {
        "type": "regex",
        "facet": None,
        "value": r"https:\/\/example\.com\/users\/.*",
    }


@pytest.mark.parametrize(
    "query,expected",
    [
        (
            "published:[2024-01-01 TO 2024-12-31]",
            {
                "type": "facet",
                "facet": "published",
                "expr": {
                    "type": "range",
                    "lower": "2024-01-01",
                    "upper": "2024-12-31",
                    "include_lower": True,
                    "include_upper": True,
                },
            },
        ),
        (
            "followers:{10 TO 20}",
            {
                "type": "facet",
                "facet": "followers",
                "expr": {
                    "type": "range",
                    "lower": "10",
                    "upper": "20",
                    "include_lower": False,
                    "include_upper": False,
                },
            },
        ),
        (
            "rank:[10 TO 20}",
            {
                "type": "facet",
                "facet": "rank",
                "expr": {
                    "type": "range",
                    "lower": "10",
                    "upper": "20",
                    "include_lower": True,
                    "include_upper": False,
                },
            },
        ),
    ],
)
def test_field_ranges(query, expected):
    assert parse_query(query) == expected


def test_field_group():
    ast = parse_query("tag:(activitypub search)")
    # tag: (activitypub AND search)
    assert ast["type"] == "facet"
    assert ast["facet"] == "tag"
    inner = ast["expr"]
    assert inner["type"] == "bool"
    assert inner["op"] == "AND"
    assert inner["left"]["value"] == "activitypub"
    assert inner["right"]["value"] == "search"


def test_field_and_type_combination():
    query = "preferredUsername:ev* type:Person"
    ast = parse_query(query)

    assert ast == {
        "type": "bool",
        "op": "AND",
        "left": {
            "type": "facet",
            "facet": "preferredUsername",
            "expr": {
                "type": "term",
                "facet": None,
                "value": "ev*",
            },
        },
        "right": {
            "type": "facet",
            "facet": "type",
            "expr": {
                "type": "term",
                "facet": None,
                "value": "Person",
            },
        },
    }


def test_wildcard_term():
    ast = parse_query("fed*")
    assert ast == {
        "type": "term",
        "facet": None,
        "value": "fed*",
    }


def test_complex_example_from_fep():
    query = 'tag:fediverse language:en "full text search"'
    ast = parse_query(query)
    # ((tag:fediverse AND language:en) AND "full text search")
    assert ast["type"] == "bool"
    assert ast["op"] == "AND"

    left = ast["left"]
    right = ast["right"]

    # right side is phrase
    assert right["type"] == "phrase"
    assert right["value"] == "full text search"

    # left side is (tag:fediverse AND language:en)
    assert left["type"] == "bool"
    assert left["op"] == "AND"
    assert left["left"]["type"] == "facet"
    assert left["left"]["facet"] == "tag"
    assert left["left"]["expr"]["value"] == "fediverse"
    assert left["right"]["type"] == "facet"
    assert left["right"]["facet"] == "language"
    assert left["right"]["expr"]["value"] == "en"


@pytest.mark.parametrize(
    "query",
    [
        "(",  # missing closing paren
        "tag:",  # missing value
        "tag:(cats OR",  # incomplete group
        "/cats",  # unterminated regex
    ],
)
def test_invalid_queries_raise(query):
    with pytest.raises(ValueError):
        parse_query(query)
