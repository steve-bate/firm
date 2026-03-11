import re
from dataclasses import dataclass
from typing import List, Optional

# ---------- Token definitions ----------

TOKEN_SPEC = [
    ("SPACE", r"[ \t\r\n]+"),
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("LBRACK", r"\["),
    ("RBRACK", r"\]"),
    ("LBRACE", r"\{"),
    ("RBRACE", r"\}"),
    ("COLON", r":"),
    ("AND", r"AND\b"),
    ("OR", r"OR\b"),
    ("NOT", r"NOT\b"),
    ("TO", r"TO\b"),
    ("REGEX", r"/(?:[^/\\]|\\.)*/"),  # /foo.*/
    ("PHRASE", r'"([^"]*)"'),  # "foo bar"
    ("WORD", r'[^ \t\r\n()\[\]{}":/]+'),  # terms, fields, wildcards
]

TOK_REGEX = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in TOKEN_SPEC))


SMART_QUOTES_TRANSLATION = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
    }
)


@dataclass
class Token:
    kind: str
    value: str


def tokenize(q: str) -> List[Token]:
    q = q.translate(SMART_QUOTES_TRANSLATION)
    tokens: list[Token] = []
    pos = 0
    while pos < len(q):
        m = TOK_REGEX.match(q, pos)
        if m is None:
            raise ValueError(f"Unexpected character: {q[pos]}")
        kind = m.lastgroup
        if kind == "SPACE":
            pos = m.end()
            continue
        if kind == "PHRASE":
            val = m.group(0)[1:-1]
        elif kind == "REGEX":
            val = m.group(0)[1:-1]
        else:
            val = m.group(0)
        if kind is None:
            raise ValueError(f"Unknown token kind for value: {val}")
        tokens.append(Token(kind, val))
        pos = m.end()
    return tokens


# ---------- AST node helpers ----------


def term_node(value: str, field: Optional[str] = None):
    return {"type": "term", "field": field, "value": value}


def phrase_node(value: str, field: Optional[str] = None):
    return {"type": "phrase", "field": field, "value": value}


def regex_node(value: str, field: Optional[str] = None):
    return {"type": "regex", "field": field, "value": value}


def bool_node(op: str, left, right):
    return {"type": "bool", "op": op, "left": left, "right": right}


def not_node(expr):
    return {"type": "not", "expr": expr}


def field_node(field: str, expr):
    # field:term or field:(subexpr)
    return {"type": "field", "field": field, "expr": expr}


def range_node(
    lower: str,
    upper: str,
    include_lower: bool,
    include_upper: bool,
):
    return {
        "type": "range",
        "lower": lower,
        "upper": upper,
        "include_lower": include_lower,
        "include_upper": include_upper,
    }


# ---------- Recursive descent parser ----------
#
# Grammar (simplified):
#
#   query   := or_expr
#   or_expr := and_expr (OR and_expr)*
#   and_expr:= unary (AND unary | implicit_and unary)*
#   unary   := NOT unary
#            | primary
#   primary := term
#            | phrase
#            | range
#            | field_expr
#            | LPAREN query RPAREN
#
#   field_expr := WORD ':' (primary | LPAREN query RPAREN)
#
# Default operator between adjacent primaries is AND.
# ----------


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[Token]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def consume(self, kind: Optional[str] = None) -> Token:
        tok = self.peek()
        if tok is None:
            raise ValueError("Unexpected end of input")
        if kind and tok.kind != kind:
            raise ValueError(f"Expected {kind}, got {tok.kind} ({tok.value})")
        self.pos += 1
        return tok

    def parse(self):
        if not self.tokens:
            return None
        expr = self.parse_or()
        if self.peek() is not None:
            raise ValueError(f"Unexpected token: {self.peek().value}")
        return expr

    def parse_or(self):
        left = self.parse_and()
        while True:
            tok = self.peek()
            if tok and tok.kind == "OR":
                self.consume("OR")
                right = self.parse_and()
                left = bool_node("OR", left, right)
            else:
                break
        return left

    def parse_and(self):
        left = self.parse_unary()
        while True:
            tok = self.peek()
            if tok and tok.kind == "AND":
                self.consume("AND")
                right = self.parse_unary()
                left = bool_node("AND", left, right)
            # implicit AND: term term -> term AND term
            elif tok and tok.kind in (
                "WORD",
                "PHRASE",
                "REGEX",
                "LPAREN",
                "LBRACK",
                "LBRACE",
                "NOT",
            ):
                right = self.parse_unary()
                left = bool_node("AND", left, right)
            else:
                break
        return left

    def parse_unary(self):
        tok = self.peek()
        if tok and tok.kind == "NOT":
            self.consume("NOT")
            expr = self.parse_unary()
            return not_node(expr)
        return self.parse_primary()

    def parse_primary(self):
        tok = self.peek()
        if tok is None:
            raise ValueError("Unexpected end of input in primary")

        if tok.kind in ("LBRACK", "LBRACE"):
            return self.parse_range()

        if tok.kind == "LPAREN":
            self.consume("LPAREN")
            expr = self.parse_or()
            if self.peek() is None or self.peek().kind != "RPAREN":
                raise ValueError("Missing closing parenthesis")
            self.consume("RPAREN")
            return expr

        if tok.kind == "PHRASE":
            self.consume("PHRASE")
            return phrase_node(tok.value)

        if tok.kind == "REGEX":
            self.consume("REGEX")
            return regex_node(tok.value)

        if tok.kind == "WORD":
            # Could be a field: ... or a plain term
            # Look ahead for COLON
            field_tok = tok
            self.consume("WORD")
            if self.peek() and self.peek().kind == "COLON":
                # fielded expression
                self.consume("COLON")
                # field:LPAREN query RPAREN
                if self.peek() and self.peek().kind == "LPAREN":
                    self.consume("LPAREN")
                    inner = self.parse_or()
                    if self.peek() is None or self.peek().kind != "RPAREN":
                        raise ValueError("Missing closing parenthesis after field group")
                    self.consume("RPAREN")
                    return field_node(field_tok.value, inner)
                else:
                    # field:term or field:"phrase" or field:/regex/ or field:[a TO b]
                    inner_tok = self.peek()
                    if inner_tok is None:
                        raise ValueError("Expected term, phrase, or regex after field:")
                    if inner_tok.kind in ("LBRACK", "LBRACE"):
                        return field_node(field_tok.value, self.parse_range())
                    if inner_tok.kind == "PHRASE":
                        self.consume("PHRASE")
                        return field_node(field_tok.value, phrase_node(inner_tok.value))
                    elif inner_tok.kind == "REGEX":
                        self.consume("REGEX")
                        return field_node(field_tok.value, regex_node(inner_tok.value))
                    elif inner_tok.kind == "WORD":
                        self.consume("WORD")
                        return field_node(field_tok.value, term_node(inner_tok.value))
                    else:
                        raise ValueError(f"Unexpected token after field: {inner_tok.value}")
            else:
                # plain term
                return term_node(field_tok.value)

        raise ValueError(f"Unexpected token in primary: {tok.value}")

    def parse_range(self):
        opening = self.peek()
        if opening is None or opening.kind not in ("LBRACK", "LBRACE"):
            raise ValueError("Range must start with '[' or '{'")

        self.consume(opening.kind)

        lower_tok = self.peek()
        if lower_tok is None or lower_tok.kind not in ("WORD", "PHRASE"):
            raise ValueError("Expected lower range bound")
        self.consume(lower_tok.kind)

        self.consume("TO")

        upper_tok = self.peek()
        if upper_tok is None or upper_tok.kind not in ("WORD", "PHRASE"):
            raise ValueError("Expected upper range bound")
        self.consume(upper_tok.kind)

        closing = self.peek()
        if closing is None or closing.kind not in ("RBRACK", "RBRACE"):
            raise ValueError("Missing closing range delimiter")
        self.consume(closing.kind)

        return range_node(
            lower=lower_tok.value,
            upper=upper_tok.value,
            include_lower=(opening.kind == "LBRACK"),
            include_upper=(closing.kind == "RBRACK"),
        )


def parse_query(query: str):
    tokens = tokenize(query)
    parser = Parser(tokens)
    return parser.parse()
