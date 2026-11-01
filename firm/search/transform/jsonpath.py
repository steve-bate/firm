"""RFC 9535 item filters, evaluated one document at a time.

A filtered collection page asks "what does this item select?" of each candidate, so the
matcher takes a single document. It lives here rather than in the service because
``firm.core.store`` may import ``firm.search`` but never ``firm.core.services``.
"""

from typing import cast

import jsonpath_rfc9535 as jsonpath

from firm.core.interfaces import JSON, JSONObject


def item_filter(expr: str) -> str:
    """A caller's filter text as an RFC 9535 path over the item list."""
    expr = (
        expr if expr.startswith("$") else f"$[?{expr}]" if not expr.startswith("[") else f"${expr}"
    )
    return expr


class JsonPathMatcher:
    """What one collection item selects under the caller's filter expression.

    The expression is rooted at a *single item*, not at the collection, because fep-c195
    defines the filter as item-level — "applied to collection items, not to the outer
    collection object". The matcher is handed one document at a time and ``values`` answers
    "what does this expression select from it?", so a page costs one question per candidate
    examined and no question needs the item list.

    ``values`` returns the selection rather than a boolean because an expression may project:
    ``[?@.object[*].attachment[?@.type == 'Video']].id`` selects the item's ``id``, and that
    value is what a filtered read returns for the item. With no trailing path the selection is
    the item itself, so one call serves both spellings:

    - ``$[?@.type == 'Create']`` on a matching item -> ``[<the item>]``
    - ``$[?@.type == 'Create'].id`` on it -> ``[<its id>]``

    A document from which the expression selects nothing is not a match. That question is
    asked per item, so a root-level index or slice selector (``$[0:2]``, ``$[-1]``) selects
    the item out of its own one-element list and matches every candidate: a surprising
    filter, but a consistent one — the expression is evaluated the same way for every item,
    and fep-c195 requires neither reading.

    This is deliberately not how the unpaged branch filtered. That ran
    ``jsonpath.find(expression, items)`` over the whole dereferenced list and installed the
    returned *nodes* as the collection's items, so its answer depended on the shape of the
    list rather than on the item.
    """

    def __init__(self, expression: str) -> None:
        path = item_filter(expression)
        try:
            self._compiled = jsonpath.compile(path)
        except jsonpath.JSONPathError as e:
            # fep-c195 (Error Handling): a parse failure is a client error, not a 500, and
            # JSONPathSyntaxError is not a ValueError, so the service would otherwise 500.
            raise ValueError(f"Invalid JSONPath filter {expression!r}: {e}") from None

    def values(self, document: JSONObject) -> list[JSON]:
        """What the expression selects from one item: the item itself, or a projection."""
        return cast(list[JSON], [node.value for node in self._compiled.find([document])])

    def is_match(self, document: JSONObject) -> bool:
        return bool(self.values(document))
