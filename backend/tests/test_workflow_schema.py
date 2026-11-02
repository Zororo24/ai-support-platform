"""Workflow definitions: structural validation and the shipped templates."""

import copy

import pytest
from pydantic import ValidationError
from seed.seed import demo_actions

from app.agents.action_schema import ActionDef
from app.agents.config import AgentConfig
from app.agents.workflow import coerce
from app.agents.workflow_schema import FieldDef, WorkflowDef, check_against_actions
from app.agents.workflow_templates import TEMPLATES, readiness

CONN = "00000000-0000-0000-0000-000000000001"


def demo():
    return [ActionDef.model_validate(a) for a in demo_actions(CONN)]


def simple(**overrides):
    base = {
        "name": "intake",
        "description": "Collect a name and say hello.",
        "start": "ask",
        "nodes": {
            "ask": {"type": "collect", "fields": [{"name": "who", "ask": "Who are you?"}], "next": "hello"},
            "hello": {"type": "message", "text": "Hi {{vars.who}}!"},
        },
    }
    base.update(overrides)
    return base


def test_all_shipped_templates_are_valid_for_the_demo_actions() -> None:
    actions = {a.name: a for a in demo()}
    for template in TEMPLATES:
        wf = WorkflowDef.model_validate(template["workflow"])
        check_against_actions(wf, actions)
        assert readiness(template, list(actions.values())) == []
        # and the whole agent config accepts it
        AgentConfig.model_validate({"actions": demo_actions(CONN), "workflows": [template["workflow"]]})


def test_readiness_names_what_is_missing() -> None:
    template = next(t for t in TEMPLATES if t["id"] == "return_request")
    assert readiness(template, []) == ["an action called 'get_order'", "an action called 'create_return'"]
    trimmed = [a for a in demo() if a.name != "create_return"]
    assert readiness(template, trimmed) == ["an action called 'create_return'"]


def mutate(path_fn):
    wf = simple()
    path_fn(wf)
    return wf


@pytest.mark.parametrize(
    "build,message",
    [
        (lambda: simple(start="nowhere"), "there is no step called 'nowhere'"),
        (lambda: mutate(lambda w: w["nodes"]["ask"].update(next="ghost")), "there is no step called 'ghost'"),
        (
            lambda: mutate(lambda w: w["nodes"].update(orphan={"type": "message", "text": "x"})),
            "never be reached",
        ),
        (lambda: mutate(lambda w: w["nodes"]["hello"].update(text="Hi {{vars.whom}}")), "no step ever sets"),
        (lambda: mutate(lambda w: w["nodes"]["hello"].update(text="Hi {{args.x}}")), "vars. or context."),
        (
            lambda: mutate(lambda w: w["nodes"]["hello"].update(text="Hi {{vars.who + 1}}")),
            "plain variable path",
        ),
        (
            lambda: simple(nodes={"ask": {"type": "branch", "cases": [{"when": "vars. >", "next": "ask"}]}}),
            "condition",
        ),
        (
            lambda: mutate(
                lambda w: (
                    w["nodes"].update(
                        b={"type": "branch", "cases": [{"when": "vars.nope > 1", "next": "hello"}]}
                    )
                    or w["nodes"]["ask"].update(next="b")
                )
            ),
            "which no step sets",
        ),
        (lambda: mutate(lambda w: w["nodes"]["ask"]["fields"][0].update(name="Bad Name")), "lowercase"),
        (
            lambda: mutate(lambda w: w["nodes"]["ask"]["fields"][0].update(pattern="(unclosed")),
            "invalid pattern",
        ),
        (lambda: mutate(lambda w: w["nodes"]["ask"].update(fields=[])), "at least 1"),
        (
            lambda: mutate(lambda w: w["nodes"].update(**{"Bad-Id": {"type": "message", "text": "x"}})),
            "step id",
        ),
        (lambda: mutate(lambda w: w["nodes"].update(ok={"type": "teleport"})), "union_tag_invalid"),
        (lambda: mutate(lambda w: w["nodes"]["hello"].update(surprise=1)), "Extra inputs"),
        (lambda: simple(name="Not Valid"), "lowercase"),
        (
            lambda: simple(
                nodes={f"n{i}": {"type": "message", "text": "x", "next": f"n{i + 1}"} for i in range(41)}
                | {"n41": {"type": "end"}},
                start="n0",
            ),
            "at most 40",
        ),
    ],
)
def test_invalid_workflows_are_rejected_with_a_reason(build, message) -> None:
    with pytest.raises(ValidationError) as exc:
        WorkflowDef.model_validate(build())
    assert message in str(exc.value)


def test_confirmed_by_must_point_at_a_confirm_step() -> None:
    wf = copy.deepcopy(next(t for t in TEMPLATES if t["id"] == "ticket_intake")["workflow"])
    wf["nodes"]["create"]["confirmed_by"] = "details"
    with pytest.raises(ValidationError) as exc:
        WorkflowDef.model_validate(wf)
    assert "confirmed_by" in str(exc.value)


def test_loops_are_allowed_in_the_definition() -> None:
    wf = {
        "name": "again", "description": "Loops until told otherwise.", "start": "a",
        "nodes": {"a": {"type": "branch", "cases": [{"when": "true", "next": "a"}]}},
    }  # fmt: skip
    WorkflowDef.model_validate(wf)  # bounded at runtime by the step limit, not forbidden here


@pytest.mark.parametrize(
    "mutator,message",
    [
        (lambda w: w["nodes"]["lookup"].update(action="ghost"), "does not exist"),
        (lambda w: w["nodes"]["lookup"]["args"].update(surprise="x"), "not a parameter"),
        (lambda w: w["nodes"]["lookup"].update(args={}), "needs 'order_number'"),
    ],
)
def test_action_steps_are_checked_against_the_agents_actions(mutator, message) -> None:
    wf = copy.deepcopy(next(t for t in TEMPLATES if t["id"] == "order_status")["workflow"])
    mutator(wf)
    with pytest.raises(ValidationError) as exc:
        AgentConfig.model_validate({"actions": demo_actions(CONN), "workflows": [wf]})
    assert message in str(exc.value)


def test_duplicate_workflow_names_are_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        AgentConfig.model_validate({"workflows": [simple(), simple()]})
    assert "duplicate name" in str(exc.value)


# ---------- field coercion ----------


def field(**kw):
    return FieldDef.model_validate({"name": "x", "ask": "?", **kw})


@pytest.mark.parametrize(
    "kw,raw,expected",
    [
        ({}, "  hello ", "hello"),
        ({"transform": "upper"}, "acm-1", "ACM-1"),
        ({"pattern": "^[A-Z]{3}-[0-9]+$", "transform": "upper"}, "acm-12", "ACM-12"),
        ({"enum": ["Red", "Blue"]}, "blue", "Blue"),
        ({"type": "integer", "minimum": 1, "maximum": 5}, "3", 3),
        ({"type": "number"}, "2.5", 2.5),
        ({"type": "boolean"}, "Yes", True),
        ({"type": "boolean"}, "no", False),
    ],
)
def test_coerce_accepts_and_normalises(kw, raw, expected) -> None:
    value, error = coerce(raw, field(**kw))
    assert error is None and value == expected


@pytest.mark.parametrize(
    "kw,raw",
    [
        ({"pattern": "^[0-9]+$"}, "abc"),
        ({"enum": ["a", "b"]}, "c"),
        ({"type": "integer"}, "three"),
        ({"type": "integer", "minimum": 1}, "0"),
        ({"type": "boolean"}, "maybe"),
        ({"max_length": 3}, "toolong"),
        ({}, "   "),
        ({}, {"nested": 1}),
    ],
)
def test_coerce_rejects_with_the_configured_message(kw, raw) -> None:
    value, error = coerce(raw, field(invalid_message="Nope.", **kw))
    assert value is None and error.startswith("Nope.")
