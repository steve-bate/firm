"""
High‑level RFC 9535 JSONPath coverage for jsonpath_rfc9535.

This module is organized roughly by feature area, not by RFC section,
but the docstrings reference the relevant concepts from the spec. [web:10][web:28]

Requires:
    pip install jsonpath-rfc9535
"""

import jsonpath_rfc9535 as jsonpath  # type: ignore

# ---------------------------------------------------------------------------
# Shared sample documents
# ---------------------------------------------------------------------------

STORE_DOC = {
    "store": {
        "book": [
            {
                "category": "reference",
                "author": "Nigel Rees",
                "title": "Sayings of the Century",
                "price": 8.95,
            },
            {
                "category": "fiction",
                "author": "Evelyn Waugh",
                "title": "Sword of Honour",
                "price": 12.99,
            },
            {
                "category": "fiction",
                "author": "Herman Melville",
                "title": "Moby Dick",
                "isbn": "0-553-21311-3",
                "price": 8.99,
            },
            {
                "category": "fiction",
                "author": "J. R. R. Tolkien",
                "title": "The Lord of the Rings",
                "isbn": "0-395-19395-8",
                "price": 22.99,
            },
        ],
        "bicycle": {"color": "red", "price": 19.95},
    },
    "expensive": 10,
}

ROOT_ARRAY_DOC = [
    {"type": "a", "value": 1},
    {"type": "b", "value": 2},
    {"type": "c", "value": 3},
]

MIXED_DOC = {
    "numbers": [0, 1, 2, 3, 4],
    "object": {"x": 1, "y": 2},
    "nested": [{"z": 1}, {"z": 2, "w": 3}],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def values(nodes):
    """Return plain values from a list of JSONPathNode objects."""
    return [n.value for n in nodes]


def paths(nodes):
    """Return normalized JSONPath strings for nodes, if available."""
    # jsonpath_rfc9535 exposes a path() method for nodes. [web:8]
    return [n.path() for n in nodes]


# ---------------------------------------------------------------------------
# Root, basic selectors, and singular queries
# ---------------------------------------------------------------------------


def test_root_selects_entire_document():
    """
    RFC 9535: The root node identifier '$' selects the root of the query argument. [web:28]
    """
    nodes = jsonpath.find("$", STORE_DOC)
    assert values(nodes) == [STORE_DOC]


def test_simple_member_selection_dot_and_bracket():
    """
    RFC 9535: Member selectors can use dot-notation or bracket-notation. [web:28]
    """
    dot_nodes = jsonpath.find("$.store.bicycle.color", STORE_DOC)
    bracket_nodes = jsonpath.find("$['store']['bicycle']['color']", STORE_DOC)
    assert values(dot_nodes) == ["red"]
    assert values(bracket_nodes) == ["red"]


def test_singular_query_fixed_path():
    """
    RFC 9535: A 'singular query' can select at most one node for any JSON value. [web:2]
    Here we use only fixed member names and indexes.
    """
    nodes = jsonpath.find("$.store.book[0].title", STORE_DOC)
    assert values(nodes) == ["Sayings of the Century"]


# ---------------------------------------------------------------------------
# Arrays: index, union, and slices
# ---------------------------------------------------------------------------


def test_array_index_single_element():
    """
    RFC 9535: Index selectors select a single array element by position. [web:28]
    """
    nodes = jsonpath.find("$.store.book[1].title", STORE_DOC)
    assert values(nodes) == ["Sword of Honour"]


def test_array_index_union_multiple_positions():
    """
    RFC 9535: Index unions select multiple array positions: [i0, i1, ...]. [web:28]
    """
    nodes = jsonpath.find("$.store.book[0,2].title", STORE_DOC)
    assert values(nodes) == ["Sayings of the Century", "Moby Dick"]


def test_array_slice_basic():
    """
    RFC 9535: Slice selectors [start:end:step] select ranges of array elements. [web:28]
    """
    nodes = jsonpath.find("$.numbers[1:4]", MIXED_DOC)
    assert values(nodes) == [1, 2, 3]


def test_array_slice_with_step_and_open_bounds():
    """
    RFC 9535: Slices may omit start or end and may specify step. [web:28]
    """
    nodes = jsonpath.find("$.numbers[:5:2]", MIXED_DOC)
    assert values(nodes) == [0, 2, 4]


# ---------------------------------------------------------------------------
# Wildcards
# ---------------------------------------------------------------------------


def test_member_wildcard_on_object():
    """
    RFC 9535: '*' applied to an object selects all member values. [web:2]
    """
    nodes = jsonpath.find("$.object.*", MIXED_DOC)
    # Order of object members is preserved as in the JSON value. [web:28]
    assert values(nodes) == [1, 2]


def test_array_wildcard_on_array():
    """
    RFC 9535: '*' applied to an array selects all elements. [web:2]
    """
    nodes = jsonpath.find("$.store.book[*].title", STORE_DOC)
    assert values(nodes) == [
        "Sayings of the Century",
        "Sword of Honour",
        "Moby Dick",
        "The Lord of the Rings",
    ]


# ---------------------------------------------------------------------------
# Recursive descent
# ---------------------------------------------------------------------------


def test_recursive_descent_member_any_depth():
    """
    RFC 9535: Recursive descent '..name' finds members with that name at any depth. [web:28]
    """
    nodes = jsonpath.find("$..price", STORE_DOC)
    assert values(nodes) == [8.95, 12.99, 8.99, 22.99, 19.95]


def test_recursive_descent_full_tree():
    """
    RFC 9535: '$..*' selects all descendant member values and array elements. [web:2]
    This is a smoke test that it returns a non-empty superset of other queries.
    """
    nodes = jsonpath.find("$..*", STORE_DOC)
    vals = values(nodes)
    # Sanity checks: some known values must be included.
    assert 19.95 in vals
    assert "Sayings of the Century" in vals
    assert "The Lord of the Rings" in vals


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_filter_on_numeric_comparison():
    """
    RFC 9535: Filter selectors use '[? <expr> ]' with '@' as the current node. [web:28]
    Example: select books cheaper than 'expensive'. [web:2]
    """
    nodes = jsonpath.find("$.store.book[?@.price < $.expensive]", STORE_DOC)
    assert [b["title"] for b in values(nodes)] == [
        "Sayings of the Century",
        "Moby Dick",
    ]


def test_filter_on_string_equality():
    """
    RFC 9535: Filter expressions support equality comparison on member values. [web:28]
    """
    nodes = jsonpath.find("$.store.book[?@.author == 'Herman Melville']", STORE_DOC)
    assert [b["title"] for b in values(nodes)] == ["Moby Dick"]


def test_filter_with_logical_and():
    """
    RFC 9535: Filters support '&&' and '||' logical operators. [web:28]
    """
    nodes = jsonpath.find("$.store.book[?@.category == 'fiction' && @.price > 10]", STORE_DOC)
    assert [b["title"] for b in values(nodes)] == [
        "Sword of Honour",
        "The Lord of the Rings",
    ]


def test_filter_with_parentheses_precedence():
    """
    RFC 9535: Parentheses can group subexpressions in filters. [web:28]
    """
    # (category == 'fiction' && price < 10) || (category == 'reference')
    nodes = jsonpath.find(
        "$.store.book[?(@.category == 'fiction' && @.price < 10) || " "@.category == 'reference']",
        STORE_DOC,
    )
    assert [b["title"] for b in values(nodes)] == [
        "Sayings of the Century",
        "Moby Dick",
    ]


def test_filter_over_root_array():
    """
    RFC 9535: Filters also apply when the query argument root is an array. [web:28]
    """
    nodes = jsonpath.find("$[?@.value > 1]", ROOT_ARRAY_DOC)
    assert values(nodes) == [
        {"type": "b", "value": 2},
        {"type": "c", "value": 3},
    ]


def test_filter_then_project_single_member():
    """
    RFC 9535: A member selector can follow a filter to project a single property. [web:28]
    """
    nodes = jsonpath.find("$[?@.value > 1].type", ROOT_ARRAY_DOC)
    assert values(nodes) == ["b", "c"]


# ---------------------------------------------------------------------------
# Unions (indexes and names)
# ---------------------------------------------------------------------------


def test_index_union_on_root_array():
    """
    RFC 9535: Index union '[i0,i1,...]' selects multiple positions from arrays. [web:28]
    """
    nodes = jsonpath.find("$[0,2].type", ROOT_ARRAY_DOC)
    assert values(nodes) == ["a", "c"]


def test_name_union_on_object():
    """
    RFC 9535: Name unions select multiple members of an object. [web:28]
    """
    nodes = jsonpath.find("$.store.bicycle['color','price']", STORE_DOC)
    assert values(nodes) == ["red", 19.95]


# ---------------------------------------------------------------------------
# Normalized paths (JSONPathNode.path)
# ---------------------------------------------------------------------------


def test_normalized_paths_for_filtered_results():
    """
    RFC 9535: Normalized paths provide a canonical JSONPath for each node. [web:2]
    jsonpath_rfc9535 exposes this via node.path(). [web:8]
    """
    nodes = jsonpath.find("$.store.book[?@.price < 10]", STORE_DOC)
    ps = paths(nodes)
    # Exact normalized form is implementation‑defined but should be stable and
    # include explicit ['name'] and [index] selectors.
    assert "$['store']['book'][0]" in ps
    assert "$['store']['book'][2]" in ps


def test_normalized_paths_for_root_array():
    """
    Normalized paths from a root array use numeric indexes from '$'. [web:2]
    """
    nodes = jsonpath.find("$[?@.value == 2]", ROOT_ARRAY_DOC)
    ps = paths(nodes)
    assert ps == ["$[1]"]


# ---------------------------------------------------------------------------
# Singular vs non‑singular queries (sanity checks)
# ---------------------------------------------------------------------------


def test_singular_query_returns_at_most_one_node():
    """
    RFC 9535: A syntactically singular query cannot match more than one node. [web:2]
    """
    nodes = jsonpath.find("$.store.bicycle.price", STORE_DOC)
    assert len(nodes) == 1
    assert values(nodes) == [19.95]


def test_non_singular_query_can_return_multiple_nodes():
    """
    RFC 9535: Queries using wildcards, unions, or filters are generally non‑singular. [web:2]
    """
    nodes = jsonpath.find("$.store.book[*].price", STORE_DOC)
    assert len(nodes) == 4
    assert values(nodes) == [8.95, 12.99, 8.99, 22.99]
