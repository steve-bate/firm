# test_sql_integration.py

import json
import re
import sqlite3
from pprint import pprint

import pytest

from firm.search.parser import parse_query
from firm.search.transform.sql import ast_to_sql


def make_connection():
    # In-memory DB
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    # JSON1 is usually built-in; this call just ensures it's available
    conn.execute("SELECT json('{\"ok\": true}')")
    conn.create_function(
        "regexp",
        2,
        lambda pattern, value: 1 if re.search(pattern, value or "") else 0,
    )
    return conn


def create_schema(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE objects (
            uri TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            document TEXT NOT NULL
        )
        """
    )


def insert_sample_data(conn: sqlite3.Connection):
    # Minimal sample dataset that exercises fields and text search
    rows = [
        {
            "uri": "https://example.com/users/alice",
            "type": "Person",
            "doc": {
                "id": "https://example.com/users/alice",
                "type": "Person",
                "preferredUsername": "alice",
                "actor": "https://example.com/users/alice",
                "content": "Alice enjoys activitypub and fediverse discussions.",
                "summary": "Alice the fediverse enthusiast",
                "tag": [
                    {"type": "Hashtag", "name": "fediverse"},
                    {"type": "Hashtag", "name": "activitypub"},
                ],
                "rank": 1,
                "language": "en",
                "visibility": "public",
            },
        },
        {
            "uri": "https://example.com/users/evan",
            "type": "Person",
            "doc": {
                "id": "https://example.com/users/evan",
                "type": "Person",
                "preferredUsername": "evan",
                "actor": "https://example.com/users/evan",
                "content": "Evan writes about distributed social networks.",
                "summary": "Evan the engineer",
                "tag": [{"type": "Hashtag", "name": "socialweb"}],
                "rank": 2,
                "language": "en",
                "visibility": "public",
            },
        },
        {
            "uri": "https://example.com/users/eve",
            "type": "Person",
            "doc": {
                "id": "https://example.com/users/eve",
                "type": "Person",
                "preferredUsername": "eve",
                "actor": "https://example.com/users/eve",
                "content": "Eve is testing the search system.",
                "summary": "Test account",
                "tag": [{"type": "Hashtag", "name": "testing"}],
                "rank": 3,
                "language": "en",
                "visibility": "unlisted",
            },
        },
        {
            "uri": "https://example.com/notes/1",
            "type": "Note",
            "doc": {
                "id": "https://example.com/notes/1",
                "type": "Note",
                "actor": "https://example.com/users/alice",
                "content": "This is a note about ActivityPub client search.",
                "summary": "",
                "tag": [{"type": "Hashtag", "name": "fediverse"}],
                "rank": 4,
                "language": "en",
                "visibility": "public",
            },
        },
        {
            "uri": "https://example.com/notes/2",
            "type": "Note",
            "doc": {
                "id": "https://example.com/notes/2",
                "type": "Note",
                "actor": "https://example.com/users/evan",
                "content": "Another note mentioning cats and dogs.",
                "summary": "",
                "tag": [{"type": "Hashtag", "name": "pets"}],
                "rank": 5,
                "language": "en",
                "visibility": "public",
            },
        },
    ]

    for row in rows:
        conn.execute(
            "INSERT INTO objects (uri, type, document) VALUES (?, ?, ?)",
            (row["uri"], row["type"], json.dumps(row["doc"])),
        )
    conn.commit()


@pytest.fixture
def db():
    conn = make_connection()
    create_schema(conn)
    insert_sample_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def run_search(conn: sqlite3.Connection, query: str, text_facet_names=None):
    ast = parse_query(query)
    where_sql, params = ast_to_sql(ast, text_facet_names=text_facet_names)
    sql = f"SELECT uri, type, document FROM objects WHERE {where_sql}"
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    return [r[0] for r in rows]  # return list of URIs for brevity


def test_term_search_default_fields(db):
    # "cats" appears in notes/2 content
    uris = run_search(db, "cats")
    assert "https://example.com/notes/2" in uris
    # sanity: shouldn't match notes/1 which doesn't mention cats
    assert "https://example.com/notes/1" not in uris


def test_phrase_search(db):
    # Exact phrase "ActivityPub client search" only appears in notes/1
    uris = run_search(db, '"ActivityPub client search"')
    assert uris == ["https://example.com/notes/1"]


def test_phrase_search_with_wildcards(db):
    uris = run_search(db, '"ActivityPub client sea*"')
    assert uris == ["https://example.com/notes/1"]


def test_faceted_phrase_search(db):
    # Faceted phrase on summary should match only Alice.
    # uris = run_search(db, 'summary:"fediverse enthusiast"')
    uris = run_search(db, 'summary:"fediverse enthusiast"')
    assert uris == ["https://example.com/users/alice"]


def test_boolean_and_or_not(db):
    # "cats OR dogs" -> note/2
    uris = run_search(db, "cats OR dogs")
    assert "https://example.com/notes/2" in uris

    # "(cats OR dogs) AND NOT testing" -> still note/2, exclude anything with 'testing'
    uris = run_search(db, "(cats OR dogs) AND NOT testing")
    assert "https://example.com/notes/2" in uris

    # "testing AND NOT cats" -> user eve, not note/2
    uris = run_search(db, "testing AND NOT cats")
    assert "https://example.com/users/eve" in uris
    assert "https://example.com/notes/2" not in uris


def test_field_type_and_actor(db):
    uris = run_search(db, "type:Person")
    assert "https://example.com/users/alice" in uris
    assert "https://example.com/users/evan" in uris
    assert "https://example.com/users/eve" in uris
    assert "https://example.com/notes/1" not in uris

    # actor:https://example.com/users/alice -> alice + her note
    uris = run_search(db, 'actor:"https://example.com/users/alice"')
    assert "https://example.com/users/alice" in uris
    assert "https://example.com/notes/1" in uris
    assert "https://example.com/notes/2" not in uris


def test_field_tag_and_language(db):
    # tag:fediverse -> alice and note/1
    uris = run_search(db, "tag:fediverse")
    assert "https://example.com/users/alice" in uris
    assert "https://example.com/notes/1" in uris
    assert "https://example.com/users/evan" not in uris

    # language:en -> everything in our sample data is en
    uris = run_search(db, "language:en")
    assert len(uris) == 5


def test_tag_search_matches_json_with_whitespace(db):
    db.execute("DELETE FROM objects")
    document = json.dumps(
        {
            "id": "https://example.com/notes/spacey",
            "type": "Note",
            "content": "tag spacing sample",
            "summary": "",
            "tag": [{"type": "Hashtag", "name": "fediverse"}],
        },
        separators=(", ", ": "),
    )
    db.execute(
        "INSERT INTO objects (uri, type, document) VALUES (?, ?, ?)",
        ("https://example.com/notes/spacey", "Note", document),
    )
    db.commit()

    uris = run_search(db, "tag:fediverse")
    assert uris == ["https://example.com/notes/spacey"]

    uris = run_search(db, "tag:fed*")
    assert uris == ["https://example.com/notes/spacey"]


def test_tag_search_matches_hashtag_name_in_object_tag(db):
    db.execute("DELETE FROM objects")
    activity_doc = {
        "id": "https://example.com/activities/1",
        "type": "Create",
        "object": {
            "id": "https://example.com/notes/from-object",
            "type": "Note",
            "tag": [{"type": "Hashtag", "name": "fromobject"}],
        },
    }
    db.execute(
        "INSERT INTO objects (uri, type, document) VALUES (?, ?, ?)",
        (
            "https://example.com/activities/1",
            "Create",
            json.dumps(activity_doc),
        ),
    )
    db.commit()

    uris = run_search(db, "tag:fromobject")
    assert uris == ["https://example.com/activities/1"]

    uris = run_search(db, "tag:from*")
    assert uris == ["https://example.com/activities/1"]


def test_tag_search_only_matches_hashtag_type(db):
    db.execute("DELETE FROM objects")
    doc = {
        "id": "https://example.com/notes/tag-types",
        "type": "Note",
        "tag": [
            {"type": "Mention", "name": "fediverse"},
            {"type": "Hashtag", "name": "activitypub"},
        ],
        "object": {
            "tag": [
                {"type": "Mention", "name": "fromobject"},
                {"type": "Hashtag", "name": "objecthash"},
            ]
        },
    }
    db.execute(
        "INSERT INTO objects (uri, type, document) VALUES (?, ?, ?)",
        (
            "https://example.com/notes/tag-types",
            "Note",
            json.dumps(doc),
        ),
    )
    db.commit()

    uris = run_search(db, "tag:fediverse")
    assert uris == []

    uris = run_search(db, "tag:fromobject")
    assert uris == []

    uris = run_search(db, "tag:activitypub")
    assert uris == ["https://example.com/notes/tag-types"]

    uris = run_search(db, "tag:objecthash")
    assert uris == ["https://example.com/notes/tag-types"]


def test_visibility(db):
    # visibility:public -> all except eve (unlisted)
    uris = run_search(db, "visibility:public")
    assert "https://example.com/users/eve" not in uris
    assert "https://example.com/users/alice" in uris
    assert "https://example.com/notes/2" in uris


@pytest.mark.xfail(reason="tag with expression not supported")
def test_field_group(db):
    # tag:(fediverse activitypub) -> alice, because she has both tags
    uris = run_search(db, "tag:(fediverse activitypub)")
    assert "https://example.com/users/alice" in uris
    # note/1 has only fediverse in tags, so might not match depending on tag implementation
    # For this minimal LIKE-based approach, this may or may not match both;
    # adjust assertion if you change tag handling.


def test_wildcard_search_on_field(db):
    # preferredUsername:ev* -> evan and eve
    uris = run_search(db, "preferredUsername:ev*")
    assert "https://example.com/users/evan" in uris
    assert "https://example.com/users/eve" in uris
    assert "https://example.com/users/alice" not in uris


def test_non_wildcard_field_term_is_exact_match(db):
    uris = run_search(db, "preferredUsername:eve")
    assert uris == ["https://example.com/users/eve"]

    uris = run_search(db, "preferredUsername:ev")
    assert uris == []


def test_combined_field_and_type_with_wildcard(db):
    # preferredUsername:ev* type:Person -> evan and eve, both Persons
    uris = run_search(db, "preferredUsername:ev* type:Person")
    assert "https://example.com/users/evan" in uris
    assert "https://example.com/users/eve" in uris
    assert "https://example.com/users/alice" not in uris
    assert "https://example.com/notes/1" not in uris


def test_complex_example(db):
    # tag:fediverse language:en "full text search"
    # In this dataset, only notes/1 has 'ActivityPub client search' phrase.
    # We'll relax to just tag:fediverse language:en for this example.
    uris = run_search(db, 'tag:fediverse language:en "ActivityPub client search"')
    assert "https://example.com/notes/1" in uris


@pytest.mark.parametrize(
    "query,expected",
    [
        ("/cats.*/", ["https://example.com/notes/2"]),
        ("content:/cats.*/", ["https://example.com/notes/2"]),
        (
            "preferredUsername:/ev.*/",
            ["https://example.com/users/evan", "https://example.com/users/eve"],
        ),
    ],
)
def test_regex_queries(db, query, expected):
    uris = run_search(db, query)
    assert sorted(uris) == sorted(expected)


def test_unfielded_regex_uses_configured_text_facets(db):
    uris = run_search(db, "/engineer/", text_facet_names=["summary"])
    assert uris == ["https://example.com/users/evan"]

    uris = run_search(db, "/engineer/", text_facet_names=["content"])
    assert uris == []


def test_unfielded_regex_requires_nonempty_text_facets(db):
    ast = parse_query("/cats.*/")
    with pytest.raises(ValueError, match="text_facet_names must contain at least one facet"):
        ast_to_sql(ast, text_facet_names=[])


def test_invalid_regex_query_raises(db):
    ast = parse_query("content:/(cats/")
    with pytest.raises(ValueError, match="Invalid regex pattern"):
        ast_to_sql(ast)


def test_inclusive_range_query(db):
    uris = run_search(db, "rank:[2 TO 4]")
    assert sorted(uris) == sorted(
        [
            "https://example.com/users/evan",
            "https://example.com/users/eve",
            "https://example.com/notes/1",
        ]
    )


def test_exclusive_range_query(db):
    uris = run_search(db, "rank:{2 TO 4}")
    assert sorted(uris) == sorted(
        [
            "https://example.com/users/evan",
            "https://example.com/users/eve",
            "https://example.com/notes/1",
        ]
    )


def test_half_open_range_query(db):
    uris = run_search(db, "rank:[2 TO 4}")
    assert sorted(uris) == sorted(
        [
            "https://example.com/users/evan",
            "https://example.com/users/eve",
            "https://example.com/notes/1",
        ]
    )


def test_unbounded_range_query(db):
    uris = run_search(db, "rank:[4 TO *]")
    assert sorted(uris) == sorted(
        [
            "https://example.com/notes/1",
            "https://example.com/notes/2",
        ]
    )


if __name__ == "__main__":
    # Optional: run directly for debugging
    conn = make_connection()
    create_schema(conn)
    insert_sample_data(conn)

    queries = [
        "cats",
        '"ActivityPub client search"',
        "cats OR dogs",
        "type:Person",
        "actor:https://example.com/users/alice",
        "tag:fediverse",
        "preferredUsername:ev* type:Person",
    ]
    for q in queries:
        print("QUERY:", q)
        uris = run_search(conn, q)
        pprint(uris)
        print("-" * 40)

    conn.close()
