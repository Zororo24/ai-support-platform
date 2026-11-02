"""A deliberately tiny, safe expression language for guards and workflow conditions.

    args.amount_cents > 20000 and context.plan in ['pro', 'team']
    vars.order.days_since_delivery <= 30
    not (vars.status == "closed")

There is no `eval`, no function calls and no attribute access on objects: paths only walk
dicts and lists. Anything missing evaluates to null, and comparisons with null or with
incompatible types are simply false, so a hostile or malformed payload cannot raise
surprising errors or reach interpreter internals.
"""

import re
from dataclasses import dataclass
from typing import Any

MAX_EXPRESSION_CHARS = 500
MAX_NODES = 200
MAX_DEPTH = 20
MAX_STRING_CHARS = 200


class ExpressionError(ValueError):
    """The expression is malformed or too complex."""


# ---- tokenizer ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
      (?P<number>\d+(?:\.\d+)?)
    | (?P<string>"[^"\\]*"|'[^'\\]*')
    | (?P<op><=|>=|==|!=|<|>|\+|-|\*|/|\(|\)|\[|\]|,|\.)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)
_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}


@dataclass
class Token:
    kind: str  # number | string | op | ident | keyword | end
    value: Any
    pos: int


def _tokenize(source: str) -> list[Token]:
    if len(source) > MAX_EXPRESSION_CHARS:
        raise ExpressionError(f"expression is longer than {MAX_EXPRESSION_CHARS} characters")
    tokens: list[Token] = []
    pos = 0
    while pos < len(source):
        if source[pos:].strip() == "":
            break
        match = _TOKEN_RE.match(source, pos)
        if not match or match.end() == pos:
            raise ExpressionError(f"unexpected character {source[pos:].strip()[0]!r} at position {pos}")
        start = match.start(match.lastgroup)  # type: ignore[arg-type]
        if match.lastgroup == "number":
            text = match.group("number")
            tokens.append(Token("number", float(text) if "." in text else int(text), start))
        elif match.lastgroup == "string":
            body = match.group("string")[1:-1]
            if len(body) > MAX_STRING_CHARS:
                raise ExpressionError(f"string literal longer than {MAX_STRING_CHARS} characters")
            tokens.append(Token("string", body, start))
        elif match.lastgroup == "ident":
            word = match.group("ident")
            tokens.append(Token("keyword" if word in _KEYWORDS else "ident", word, start))
        else:
            tokens.append(Token("op", match.group("op"), start))
        pos = match.end()
    tokens.append(Token("end", None, len(source)))
    return tokens


# ---- parser (recursive descent) -> AST of tuples -------------------------------------------
# ("lit", value) | ("path", (segments...)) | ("list", [nodes]) | ("neg", node)
# ("not", node) | ("bin", op, left, right) | ("and"|"or", left, right)


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0
        self.nodes = 0

    def peek(self) -> Token:
        return self.tokens[self.i]

    def take(self) -> Token:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def at_op(self, *ops: str) -> bool:
        t = self.peek()
        return t.kind == "op" and t.value in ops

    def at_kw(self, *words: str) -> bool:
        t = self.peek()
        return t.kind == "keyword" and t.value in words

    def count(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ExpressionError("expression is too complex")

    def parse(self) -> tuple:
        node = self.or_expr(0)
        if self.peek().kind != "end":
            raise ExpressionError(f"unexpected {self.peek().value!r} at position {self.peek().pos}")
        return node

    def or_expr(self, depth: int) -> tuple:
        self.check_depth(depth)
        left = self.and_expr(depth + 1)
        while self.at_kw("or"):
            self.take()
            self.count()
            left = ("or", left, self.and_expr(depth + 1))
        return left

    def and_expr(self, depth: int) -> tuple:
        left = self.not_expr(depth + 1)
        while self.at_kw("and"):
            self.take()
            self.count()
            left = ("and", left, self.not_expr(depth + 1))
        return left

    def not_expr(self, depth: int) -> tuple:
        self.check_depth(depth)
        if self.at_kw("not"):
            self.take()
            self.count()
            return ("not", self.not_expr(depth + 1))
        return self.comparison(depth + 1)

    def comparison(self, depth: int) -> tuple:
        left = self.sum_expr(depth + 1)
        op = None
        if self.at_op("==", "!=", "<", "<=", ">", ">="):
            op = self.take().value
        elif self.at_kw("in"):
            self.take()
            op = "in"
        elif (
            self.at_kw("not")
            and self.tokens[self.i + 1].kind == "keyword"
            and self.tokens[self.i + 1].value == "in"
        ):
            self.take()
            self.take()
            op = "not in"
        if op is None:
            return left
        self.count()
        return ("bin", op, left, self.sum_expr(depth + 1))

    def sum_expr(self, depth: int) -> tuple:
        self.check_depth(depth)
        left = self.term(depth + 1)
        while self.at_op("+", "-"):
            op = self.take().value
            self.count()
            left = ("bin", op, left, self.term(depth + 1))
        return left

    def term(self, depth: int) -> tuple:
        left = self.unary(depth + 1)
        while self.at_op("*", "/"):
            op = self.take().value
            self.count()
            left = ("bin", op, left, self.unary(depth + 1))
        return left

    def unary(self, depth: int) -> tuple:
        self.check_depth(depth)
        if self.at_op("-"):
            self.take()
            self.count()
            return ("neg", self.unary(depth + 1))
        return self.primary(depth + 1)

    def primary(self, depth: int) -> tuple:
        self.check_depth(depth)
        token = self.take()
        self.count()
        if token.kind in ("number", "string"):
            return ("lit", token.value)
        if token.kind == "keyword" and token.value in ("true", "false", "null"):
            return ("lit", {"true": True, "false": False, "null": None}[token.value])
        if token.kind == "ident":
            return self.path(token)
        if token.kind == "op" and token.value == "(":
            inner = self.or_expr(depth + 1)
            if not self.at_op(")"):
                raise ExpressionError(f"missing ')' near position {self.peek().pos}")
            self.take()
            return inner
        if token.kind == "op" and token.value == "[":
            items: list[tuple] = []
            while not self.at_op("]"):
                items.append(self.or_expr(depth + 1))
                if self.at_op(","):
                    self.take()
                elif not self.at_op("]"):
                    raise ExpressionError(f"expected ',' or ']' near position {self.peek().pos}")
            self.take()
            return ("list", items)
        shown = "end of expression" if token.kind == "end" else repr(token.value)
        raise ExpressionError(f"unexpected {shown} at position {token.pos}")

    def path(self, first: Token) -> tuple:
        segments: list[str | int] = [first.value]
        while True:
            if self.at_op("."):
                self.take()
                nxt = self.take()
                if nxt.kind not in ("ident", "keyword"):
                    raise ExpressionError(f"expected a name after '.' at position {nxt.pos}")
                segments.append(nxt.value)
            elif self.at_op("["):
                self.take()
                index = self.take()
                if index.kind != "number" or not isinstance(index.value, int):
                    raise ExpressionError(f"expected a whole number index at position {index.pos}")
                if not self.at_op("]"):
                    raise ExpressionError(f"missing ']' near position {self.peek().pos}")
                self.take()
                segments.append(index.value)
            else:
                break
        return ("path", tuple(segments))

    def check_depth(self, depth: int) -> None:
        if depth > MAX_DEPTH * 4:  # each grammar level adds a few frames
            raise ExpressionError("expression is nested too deeply")


# ---- evaluation -----------------------------------------------------------------------------


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _walk(scope: Any, segments: tuple) -> Any:
    current = scope
    for segment in segments:
        if isinstance(segment, int):
            if isinstance(current, list) and -len(current) <= segment < len(current):
                current = current[segment]
            else:
                return None
        elif isinstance(current, dict):
            current = current.get(segment)
        else:
            return None  # never touch object attributes
        if current is None:
            return None
    return current


_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*")
_SEGMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def split_path(source: str) -> tuple | None:
    """Segments of a plain variable path like `vars.order.items[0].name`, or None if it isn't one."""
    source = source.strip()
    if not _PATH_RE.fullmatch(source) or source.split(".")[0].split("[")[0] in _KEYWORDS:
        return None
    return tuple(
        int(m.group(2)) if m.group(2) is not None else m.group(1) for m in _SEGMENT_RE.finditer(source)
    )


def lookup(scope: Any, segments: tuple) -> Any:
    """Walk dicts and lists only; anything missing is None."""
    return _walk(scope, segments)


def _compare(op: str, left: Any, right: Any) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op in ("in", "not in"):
        if isinstance(right, (list, tuple, dict)):
            found = left in right
        elif isinstance(right, str) and isinstance(left, str):
            found = left in right
        else:
            found = False
        return found if op == "in" else not found
    if _number(left) and _number(right):
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
    if isinstance(left, str) and isinstance(right, str):
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
    return False  # null or mismatched types never satisfy an ordering


def _arith(op: str, left: Any, right: Any) -> Any:
    if op == "+" and isinstance(left, str) and isinstance(right, str):
        return left + right
    if not (_number(left) and _number(right)):
        return None
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    return None if right == 0 else left / right


def _eval(node: tuple, scope: dict) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "path":
        return _walk(scope, node[1])
    if kind == "list":
        return [_eval(item, scope) for item in node[1]]
    if kind == "neg":
        value = _eval(node[1], scope)
        return -value if _number(value) else None
    if kind == "not":
        return not _eval(node[1], scope)
    if kind == "and":
        return bool(_eval(node[1], scope)) and bool(_eval(node[2], scope))
    if kind == "or":
        return bool(_eval(node[1], scope)) or bool(_eval(node[2], scope))
    if kind == "bin":
        op, left, right = node[1], _eval(node[2], scope), _eval(node[3], scope)
        if op in ("+", "-", "*", "/"):
            return _arith(op, left, right)
        return _compare(op, left, right)
    raise ExpressionError(f"unknown node {kind}")  # pragma: no cover


def _collect_paths(node: tuple, out: set[tuple]) -> None:
    kind = node[0]
    if kind == "path":
        out.add(node[1])
    elif kind in ("list",):
        for item in node[1]:
            _collect_paths(item, out)
    elif kind in ("neg", "not"):
        _collect_paths(node[1], out)
    elif kind in ("and", "or"):
        _collect_paths(node[1], out)
        _collect_paths(node[2], out)
    elif kind == "bin":
        _collect_paths(node[2], out)
        _collect_paths(node[3], out)


class Expression:
    def __init__(self, source: str):
        self.source = source
        self._ast = _Parser(_tokenize(source)).parse()

    def evaluate(self, scope: dict) -> Any:
        return _eval(self._ast, scope)

    def paths(self) -> set[tuple]:
        """Every variable path the expression reads, as tuples of segments."""
        found: set[tuple] = set()
        _collect_paths(self._ast, found)
        return found


def parse(source: str) -> Expression:
    return Expression(source)


def evaluate(source: str, scope: dict) -> Any:
    return parse(source).evaluate(scope)
