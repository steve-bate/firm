import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from firm.core.interfaces import JSONObject
from firm.search.parser import parse_query


class JsonMatcher:
    def __init__(self, query: str, text_fields: Sequence[str]):
        self.text_fields = list(text_fields)
        self._ast = parse_query(query)

    def is_match(self, document: JSONObject):
        return self._matches(self._ast, document)

    def _matches(self, ast: Any, document: JSONObject):
        """Evaluate a parsed query AST against a JSON-like document."""
        if ast is None:
            return True

        node_type = ast["type"]

        if node_type == "term":
            return self._match_unfielded_value(ast["value"], document)

        if node_type == "phrase":
            return self._match_unfielded_value(ast["value"], document)

        if node_type == "regex":
            pattern = re.compile(ast["value"])
            return any(
                pattern.search(_ensure_string(_get_field_value(document, field)))
                for field in self.text_fields
            )

        if node_type == "bool":
            left = self._matches(ast["left"], document)
            right = self._matches(ast["right"], document)
            return (left and right) if ast["op"] == "AND" else (left or right)

        if node_type == "not":
            return not self._matches(ast["expr"], document)

        if node_type == "field":
            return self._match_field_expr(ast["field"], ast["expr"], document)

        if node_type == "range":
            raise ValueError("Range expressions must be fielded, e.g. field:[a TO b]")

        raise ValueError(f"Unknown AST node type: {node_type}")

    def _match_unfielded_value(self, value, document: JSONObject):
        return any(
            _matches_term(_ensure_string(_get_field_value(document, field)), value)
            for field in self.text_fields
        )

    def _match_field_expr(self, field: str, expr, document: JSONObject):
        if field == "tag":
            tags: list[str] = []
            for location in ("tag", "object.tag"):
                values = _get_field_value(document, location)
                if isinstance(values, list):
                    tags.extend(
                        item.get("name", "")
                        for item in values
                        if isinstance(item, dict) and item.get("type") == "Hashtag"
                    )

            return _match_expression_against_values(expr, tags)

        value = _get_field_value(document, field)
        return _match_expression_against_values(expr, [value])


def _match_expression_against_values(expr, values):
    node_type = expr["type"]

    if node_type in ("term", "phrase"):
        return any(_matches_term(_ensure_string(value), expr["value"]) for value in values)

    if node_type == "regex":
        pattern = re.compile(expr["value"])
        return any(pattern.search(_ensure_string(value)) for value in values)

    if node_type == "range":
        return any(_matches_range(value, expr) for value in values)

    if node_type == "bool":
        return (
            _match_expression_against_values(expr["left"], values)
            and _match_expression_against_values(expr["right"], values)
            if expr["op"] == "AND"
            else _match_expression_against_values(expr["left"], values)
            or _match_expression_against_values(expr["right"], values)
        )

    if node_type == "not":
        return not _match_expression_against_values(expr["expr"], values)

    raise ValueError(f"Unsupported field expression node type: {node_type}")


def _matches_range(actual_value, range_node):
    if actual_value is None:
        return False

    actual = _normalize_range_value(actual_value)
    lower = range_node["lower"]
    upper = range_node["upper"]

    if lower != "*":
        lower_bound = _normalize_range_value(lower)
        if range_node["include_lower"]:
            if actual < lower_bound:
                return False
        elif actual <= lower_bound:
            return False

    if upper != "*":
        upper_bound = _normalize_range_value(upper)
        if range_node["include_upper"]:
            if actual > upper_bound:
                return False
        elif actual >= upper_bound:
            return False

    return True


def _normalize_range_value(value: int | float | str):
    if isinstance(value, (int, float)):
        return value

    if isinstance(value, str):
        if re.fullmatch(r"[+-]?\d+", value):
            return int(value)
        if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)", value):
            return float(value)

    return str(value)


def _matches_term(actual: str, query_value: str):
    regex = _wildcard_to_regex(query_value)
    return re.search(regex, actual, re.IGNORECASE) is not None


def _wildcard_to_regex(value: str):
    escaped = re.escape(value)
    escaped = escaped.replace(r"\*", ".*").replace(r"\?", ".")
    return escaped


def _ensure_string(value: Any):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _get_field_value(document: JSONObject, field: str):
    current = document
    for segment in field.split("."):
        if not isinstance(current, dict):
            return None
        value = current.get(segment)
        if isinstance(value, Mapping):
            current = cast(JSONObject, value)
        else:
            return value
    return current
