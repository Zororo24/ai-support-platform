import pytest

from app.agents.expressions import ExpressionError, evaluate, parse, split_path
from app.agents.templating import TemplateError, placeholders, render, render_json

SCOPE = {
    "args": {"amount": 250, "reason": "broken", "tags": ["a", "b"]},
    "context": {"plan": "pro", "customer_id": "cust_1"},
    "vars": {"order": {"days": 12, "items": [{"name": "headphones"}]}, "status": "open"},
}


@pytest.mark.parametrize(
    "source,expected",
    [
        ("args.amount > 200", True),
        ("args.amount > 200 and context.plan == 'pro'", True),
        ("args.amount > 200 and context.plan == 'free'", False),
        ("args.amount <= 100 or context.plan in ['pro', 'team']", True),
        ("not (vars.status == 'closed')", True),
        ("vars.order.days <= 30", True),
        ("vars.order.items[0].name == 'headphones'", True),
        ("context.plan not in ['free']", True),
        ("'bro' in args.reason", True),
        ("'x' in args.tags", False),
        ("args.amount * 2 + 10", 510),
        ("args.amount / 4", 62.5),
        ("-args.amount", -250),
        ("1 + 2 * 3", 7),
        ("(1 + 2) * 3", 9),
        ("'a' + 'b'", "ab"),
        ("true and not false", True),
        ("null == null", True),
    ],
)
def test_evaluation(source, expected) -> None:
    assert evaluate(source, SCOPE) == expected


@pytest.mark.parametrize(
    "source",
    [
        "args.missing > 3",  # null never satisfies an ordering
        "vars.nothing.deeper.still < 1",
        "args.reason > 5",  # string vs number
        "vars.order.items[9].name == 'x'",
        "args.amount / 0 > 1",
        "context.plan.upper == 'PRO'",  # no attribute access on values
    ],
)
def test_missing_or_mismatched_values_are_just_false(source) -> None:
    assert not evaluate(source, SCOPE)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "args.",
        "args.amount >",
        "(args.amount",
        "args.amount ==== 2",
        "__import__('os').system('x')",
        "args.amount.__class__",
        "open('/etc/passwd')",
        "lambda: 1",
        "args[1.5]",
        "a b",
        "x; y",
        "'unterminated",
        "$bad",
        "x = 1",
    ],
)
def test_rejects_malformed_or_dangerous_input(source) -> None:
    try:
        # Some of these parse (a path like args.amount.__class__) but can never reach internals.
        result = evaluate(source, SCOPE)
    except ExpressionError:
        return
    assert result in (None, False)


def test_limits() -> None:
    with pytest.raises(ExpressionError):
        parse("1 + " * 400 + "1")  # too long
    with pytest.raises(ExpressionError):
        parse("(" * 200 + "1" + ")" * 200)  # too deep
    with pytest.raises(ExpressionError):
        parse("'" + "a" * 300 + "'")
    with pytest.raises(ExpressionError):
        parse(" and ".join(["a"] * 150))  # too many nodes


def test_paths_are_reported_for_static_checks() -> None:
    paths = parse("args.a > 1 and (context.b == 'x' or vars.c[0].d in [args.e])").paths()
    assert paths == {("args", "a"), ("context", "b"), ("vars", "c", 0, "d"), ("args", "e")}


def test_split_path() -> None:
    assert split_path("vars.order.items[0].name") == ("vars", "order", "items", 0, "name")
    assert split_path("args") == ("args",)
    for bad in ("args.", "1abc", "a b", "a+b", "true", "null.x", "a['b']", ""):
        assert split_path(bad) is None, bad


# ---------- templates ----------


def test_single_placeholder_keeps_type_and_mixed_text_is_text() -> None:
    assert render("{{args.amount}}", SCOPE) == 250
    assert render("{{ vars.order }}", SCOPE) == SCOPE["vars"]["order"]
    assert render("Refund {{args.amount}} for {{context.customer_id}}", SCOPE) == "Refund 250 for cust_1"
    assert render("tags={{args.tags}}", SCOPE) == 'tags=["a","b"]'
    assert render("plain text", SCOPE) == "plain text"
    assert render_json({"a": ["{{args.amount}}", {"b": "{{context.plan}}"}], "n": 1}, SCOPE) == {
        "a": [250, {"b": "pro"}],
        "n": 1,
    }


def test_missing_value_is_an_error_not_an_empty_string() -> None:
    with pytest.raises(TemplateError) as exc:
        render("id={{context.nope}}", SCOPE)
    assert exc.value.missing == "context.nope"
    with pytest.raises(TemplateError):
        render("{{args.nope}}", SCOPE)


@pytest.mark.parametrize(
    "bad", ["{{ args.amount + 1 }}", "{{ len(args.tags) }}", "{{", "}}", "{{ }}", "{{args.a}} }}"]
)
def test_malformed_placeholders_are_rejected(bad) -> None:
    with pytest.raises(TemplateError):
        render(bad, SCOPE)


def test_url_quoting_blocks_path_injection() -> None:
    scope = {"args": {"id": "../admin/x?y=1#z"}}
    assert render("/orders/{{args.id}}", scope, url_quote=True) == "/orders/..%2Fadmin%2Fx%3Fy%3D1%23z"
    with pytest.raises(TemplateError):
        render("/orders/{{args.id}}", {"args": {"id": ".."}}, url_quote=True)
    assert render("/o/{{args.id}}", {"args": {"id": "a b"}}, url_quote=True) == "/o/a%20b"


def test_placeholder_listing() -> None:
    found = placeholders({"p": "/x/{{args.a}}", "b": ["{{ context.c }}", {"k": "no placeholder"}]})
    assert found == {"args.a", "context.c"}
