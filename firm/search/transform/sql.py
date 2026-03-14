# query_to_sql.py

import re
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_TEXT_FACETS = ["content", "summary"]

# Public entry point: convert AST -> (sql, params)
#
# Example:
#   ast = parse_query('preferredUsername:ev* type:Person')
#   sql, params = ast_to_sql(ast)
#
#   final_sql = f"SELECT uri, type, document FROM objects WHERE {sql}"
#   cur.execute(final_sql, params)


def ast_to_sql(
    ast: Dict[str, Any],
    text_facet_names: Optional[List[str]] = None,
) -> Tuple[str, List[Any]]:
    """
    Convert a parsed query AST (from parse_query) into a SQL WHERE clause
    and a list of parameters for sqlite3.

    Returns: (where_sql, params)
    """
    if ast is None:
        # Match everything
        return "1=1", []

    facets = text_facet_names if text_facet_names is not None else DEFAULT_TEXT_FACETS

    sql, params = _node_to_sql(ast, facets)
    return sql, params


# ---------- Internal helpers ----------


def _node_to_sql(node: Dict[str, Any], text_facet_names: List[str]) -> Tuple[str, List[Any]]:
    ntype = node["type"]

    if ntype == "term":
        return _term_to_sql(node["value"])

    if ntype == "phrase":
        return _phrase_to_sql(node["value"])

    if ntype == "regex":
        return _regex_to_sql(node["value"], text_facet_names)

    if ntype == "bool":
        return _bool_to_sql(node, text_facet_names)

    if ntype == "not":
        inner_sql, inner_params = _node_to_sql(node["expr"], text_facet_names)
        return f"NOT ({inner_sql})", inner_params

    if ntype == "facet":
        return _facet_to_sql(node)

    if ntype == "range":
        raise ValueError("Range expressions must be fielded, e.g. field:[a TO b]")

    raise ValueError(f"Unknown AST node type: {ntype}")


def _term_to_sql(value: str) -> Tuple[str, List[Any]]:
    """
    Default text term: search in main text fields from JSON, e.g.
    $.content and $.summary.
    """
    pattern, _ = _wildcard_to_like(value)
    sql = (
        "("
        "json_extract(document, '$.content') LIKE ? "
        "OR json_extract(document, '$.summary') LIKE ?"
        ")"
    )
    return sql, [pattern, pattern]


def _phrase_to_sql(value: str) -> Tuple[str, List[Any]]:
    """
    Phrase is treated similarly to term here, just LIKE '%phrase%'.
    """
    pattern = _phrase_to_like(value)
    sql = (
        "("
        "json_extract(document, '$.content') LIKE ? "
        "OR json_extract(document, '$.summary') LIKE ?"
        ")"
    )
    return sql, [pattern, pattern]


def _regex_to_sql(value: str, text_facet_names: List[str]) -> Tuple[str, List[Any]]:
    """
    Regex term: search in main text fields using sqlite regexp() function.
    """
    _validate_regex(value)
    if not text_facet_names:
        raise ValueError("text_facet_names must contain at least one facet")

    clauses: List[str] = []
    params: List[Any] = []
    for facet in text_facet_names:
        path = _facet_to_json_path(facet)
        clauses.append(f"regexp(?, coalesce(json_extract(document, '{path}'), ''))")
        params.append(value)

    return f"({' OR '.join(clauses)})", params


def _bool_to_sql(node: Dict[str, Any], text_facet_names: List[str]) -> Tuple[str, List[Any]]:
    op = node["op"]  # "AND" / "OR"
    left_sql, left_params = _node_to_sql(node["left"], text_facet_names)
    right_sql, right_params = _node_to_sql(node["right"], text_facet_names)
    sql = f"({left_sql} {op} {right_sql})"
    return sql, left_params + right_params


# TODO Example only. This needs to be generalized.
def _facet_to_sql(node: Dict[str, Any]) -> Tuple[str, List[Any]]:
    facet = node["facet"]
    expr = node["expr"]

    # Map known facets explicitly; others fall back to generic JSON path
    if facet == "type":
        if expr["type"] != "term":
            raise ValueError("type: only supports simple term in this example")
        sql = "type = ?"
        return sql, [expr["value"]]

    if facet == "actor":
        # actor:@alice@example.com
        inner_sql, params = _simple_json_field(expr, "$.actor")
        return inner_sql, params

    if facet == "preferredUsername":
        # preferredUsername:ev* → json_extract(..., '$.preferredUsername') LIKE 'ev%'
        inner_sql, params = _simple_json_field(expr, "$.preferredUsername")
        return inner_sql, params

    if facet == "tag":
        return _tag_field_to_sql(expr)

    if facet == "language":
        # language:en → json_extract(document, '$.language') = 'en'
        if expr["type"] != "term":
            raise ValueError("language: only supports simple term in this example")
        sql = "json_extract(document, '$.language') = ?"
        return sql, [expr["value"]]

    if facet == "visibility":
        # visibility:public etc.
        if expr["type"] != "term":
            raise ValueError("visibility: only supports simple term in this example")
        sql = "json_extract(document, '$.visibility') = ?"
        return sql, [expr["value"]]

    # Fallback: generic JSON path: $.<facet>
    path = f"$.{facet}"
    return _simple_json_field(expr, path)


def _simple_json_field(expr: Dict[str, Any], json_path: str) -> Tuple[str, List[Any]]:
    """
    Map a sub-expression onto json_extract(document, json_path).
    Supports term/phrase and simple boolean combinations, but in this
    minimal example we only accept term/phrase.
    """
    etype = expr["type"]
    if etype == "term":
        value = expr["value"]
        if _has_lucene_wildcard(value):
            pattern, _ = _wildcard_to_like(value)
            sql = f"json_extract(document, '{json_path}') LIKE ?"
            return sql, [pattern]

        sql = f"json_extract(document, '{json_path}') = ?"
        return sql, [value]
    if etype == "phrase":
        pattern = _phrase_to_like(expr["value"])
        sql = f"json_extract(document, '{json_path}') LIKE ?"
        return sql, [pattern]
    if etype == "regex":
        _validate_regex(expr["value"])
        sql = f"regexp(?, coalesce(json_extract(document, '{json_path}'), ''))"
        return sql, [expr["value"]]

    if etype == "range":
        column = f"json_extract(document, '{json_path}')"
        clauses: List[str] = []
        params: List[Any] = []

        lower = expr["lower"]
        upper = expr["upper"]

        if lower != "*":
            clauses.append(f"{column} >= ?")
            params.append(_coerce_range_bound(lower))

        if upper != "*":
            clauses.append(f"{column} <= ?")
            params.append(_coerce_range_bound(upper))

        if not clauses:
            return "1=1", []

        return f"({' AND '.join(clauses)})", params

    # You can extend this to field:(term1 term2) → AND etc.
    raise ValueError(f"Unsupported expression type for JSON field: {etype}")


def _tag_field_to_sql(expr: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """
    Special-case tag facet:
      - search hashtag names in $.tag[*] and $.object.tag[*]
      - only include elements where type == "Hashtag"
    """
    etype = expr["type"]
    if etype not in ("term", "phrase"):
        raise ValueError("tag: only supports term/phrase in this example")

    value = expr["value"]
    if etype == "phrase":
        comparator = "LIKE"
        parameter = _phrase_to_like(value)
    elif _has_lucene_wildcard(value):
        comparator = "LIKE"
        parameter, _ = _wildcard_to_like(value)
    else:
        comparator = "="
        parameter = value

    clause = (
        "("
        "EXISTS ("
        "SELECT 1 FROM json_each(document, '$.tag') AS tag_item "
        "WHERE json_extract(tag_item.value, '$.type') = 'Hashtag' "
        f"AND json_extract(tag_item.value, '$.name') {comparator} ?"
        ") "
        "OR EXISTS ("
        "SELECT 1 FROM json_each(document, '$.object.tag') AS tag_item "
        "WHERE json_extract(tag_item.value, '$.type') = 'Hashtag' "
        f"AND json_extract(tag_item.value, '$.name') {comparator} ?"
        ")"
        ")"
    )
    return clause, [parameter, parameter]


def _coerce_range_bound(value: str) -> Any:
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)", value):
        return float(value)
    return value


def _wildcard_to_like(value: str) -> Tuple[str, str]:
    """
    Convert lucene-like wildcards (*, ?) into SQLite LIKE wildcards (%, _),
    and wrap with % for substring match if no explicit wildcard is present.
    """
    has_lucene_wildcard = "*" in value or "?" in value
    pattern = value.replace("*", "%").replace("?", "_")
    if not has_lucene_wildcard:
        pattern = f"%{pattern}%"
    return pattern, pattern


def _phrase_to_like(value: str) -> str:
    converted, _ = _wildcard_to_like(value)
    return f"%{converted}%"


def _has_lucene_wildcard(value: str) -> bool:
    return "*" in value or "?" in value


def _validate_regex(value: str) -> None:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {value}") from exc


def _facet_to_json_path(facet: str) -> str:
    parts = facet.split(".")
    if not parts or any(not part for part in parts):
        raise ValueError(f"Invalid text facet name: {facet}")

    for part in parts:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
            raise ValueError(f"Invalid text facet name: {facet}")

    return "$." + ".".join(parts)
