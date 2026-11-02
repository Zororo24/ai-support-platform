"""Workflow definitions: guided, multi-turn processes (returns, bookings, ticket intake).

A workflow is a small directed graph with a fixed set of node types. There is no scripting: values
move between nodes as `vars`, conditions use the safe expression language, and text uses
`{{ path }}` templates. Everything is validated when the agent is saved, so a broken process is
caught by the admin, never discovered by a customer.
"""

import re
from collections import deque
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.action_schema import ActionDef
from app.agents.expressions import ExpressionError, parse, split_path
from app.agents.templating import placeholders

NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
WORKFLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_NODES = 40
ROOTS = {"vars", "context"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldDef(_Strict):
    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    ask: str = Field(min_length=1, max_length=300)
    invalid_message: str = Field(default="That doesn't look right.", max_length=200)
    pattern: str | None = Field(default=None, max_length=200)
    enum: list[str] | None = Field(default=None, max_length=50)
    transform: Literal["upper", "lower"] | None = None
    max_length: int = Field(default=500, ge=1, le=5000)
    minimum: float | None = None
    maximum: float | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not FIELD_NAME_RE.match(value):
            raise ValueError("field names are lowercase letters, digits and underscores")
        return value

    @field_validator("pattern")
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid pattern: {exc}") from exc
        return value


class CollectNode(_Strict):
    type: Literal["collect"]
    fields: list[FieldDef] = Field(min_length=1, max_length=8)
    next: str


class ActionNode(_Strict):
    type: Literal["action"]
    action: str
    args: dict[str, Any] = Field(default_factory=dict)  # values may use {{vars.x}} / {{context.x}}
    save_as: str | None = None
    on_error: str | None = None
    # A `confirm` node the customer must have approved in this run; lets a write action skip its own
    # approval question so the customer is not asked twice.
    confirmed_by: str | None = None
    next: str | None = None


class Case(_Strict):
    when: str = Field(min_length=1, max_length=500)
    next: str


class BranchNode(_Strict):
    type: Literal["branch"]
    cases: list[Case] = Field(min_length=1, max_length=10)
    otherwise: str | None = None


class LlmNode(_Strict):
    type: Literal["llm"]
    prompt: str = Field(min_length=1, max_length=2000)
    save_as: str
    fields: list[str] = Field(default_factory=list, max_length=8)  # if set, the model returns these keys
    next: str | None = None


class KnowledgeNode(_Strict):
    type: Literal["knowledge"]
    question: str = Field(min_length=1, max_length=500)
    save_as: str | None = None
    say: bool = True  # include the grounded answer in the reply
    next: str | None = None


class MessageNode(_Strict):
    type: Literal["message"]
    text: str = Field(min_length=1, max_length=2000)
    next: str | None = None


class ConfirmNode(_Strict):
    type: Literal["confirm"]
    summary: str = Field(min_length=1, max_length=500)
    yes: str
    no: str


class HandoffNode(_Strict):
    type: Literal["handoff"]
    reason: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=1000)


class EndNode(_Strict):
    type: Literal["end"]
    status: Literal["completed", "failed"] = "completed"
    message: str | None = Field(default=None, max_length=2000)


Node = Annotated[
    CollectNode
    | ActionNode
    | BranchNode
    | LlmNode
    | KnowledgeNode
    | MessageNode
    | ConfirmNode
    | HandoffNode
    | EndNode,
    Field(discriminator="type"),
]


class WorkflowDef(_Strict):
    name: str
    description: str = Field(min_length=1, max_length=300)  # the model uses this to choose the workflow
    start: str
    nodes: dict[str, Node] = Field(min_length=1)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not WORKFLOW_NAME_RE.match(value):
            raise ValueError("name must be lowercase letters, digits and underscores, starting with a letter")
        return value

    @field_validator("nodes")
    @classmethod
    def _node_ids(cls, nodes: dict[str, Any]) -> dict[str, Any]:
        if len(nodes) > MAX_NODES:
            raise ValueError(f"a workflow can have at most {MAX_NODES} steps")
        for node_id in nodes:
            if not NODE_ID_RE.match(node_id):
                raise ValueError(f"step id '{node_id}' must be lowercase letters, digits and underscores")
        return nodes

    @model_validator(mode="after")
    def _structure(self) -> "WorkflowDef":
        ids = set(self.nodes)
        if self.start not in ids:
            raise ValueError(f"start: there is no step called '{self.start}'")

        def check(target: str | None, where: str) -> None:
            if target is not None and target not in ids:
                raise ValueError(f"{where}: there is no step called '{target}'")

        for node_id, node in self.nodes.items():
            where = f"step '{node_id}'"
            for target in successors(node):
                check(target, where)
            if isinstance(node, ActionNode):
                check(node.on_error, where)
                if node.confirmed_by is not None:
                    other = self.nodes.get(node.confirmed_by)
                    if not isinstance(other, ConfirmNode):
                        raise ValueError(f"{where}: confirmed_by must name a 'confirm' step")
            if isinstance(node, BranchNode):
                for case in node.cases:
                    try:
                        parse(case.when)
                    except ExpressionError as exc:
                        raise ValueError(f"{where}: condition '{case.when}': {exc}") from exc

        # Every step must be reachable, or the admin has probably mis-wired a branch.
        seen, queue = {self.start}, deque([self.start])
        while queue:
            node = self.nodes[queue.popleft()]
            targets = list(successors(node))
            if isinstance(node, ActionNode) and node.on_error:
                targets.append(node.on_error)
            for t in targets:
                if t is not None and t not in seen:
                    seen.add(t)
                    queue.append(t)
        unreachable = sorted(ids - seen)
        if unreachable:
            raise ValueError(f"step '{unreachable[0]}' can never be reached from the start")

        # Variable names must be set somewhere in the workflow before anything can use them.
        known = assigned_vars(self)
        for node_id, node in self.nodes.items():
            for source in used_sources(node):
                segments = split_path(source)
                if segments is None:
                    raise ValueError(f"step '{node_id}': '{{{{ {source} }}}}' is not a plain variable path")
                if segments[0] not in ROOTS:
                    raise ValueError(f"step '{node_id}': '{source}' must start with vars. or context.")
                if segments[0] == "vars" and (len(segments) < 2 or segments[1] not in known):
                    raise ValueError(f"step '{node_id}': '{source}' uses a value that no step ever sets")
        for node_id, node in self.nodes.items():
            if isinstance(node, BranchNode):
                for case in node.cases:
                    for path in parse(case.when).paths():
                        if path[0] not in ROOTS:
                            raise ValueError(
                                f"step '{node_id}': condition '{case.when}' must use vars. or context."
                            )
                        if path[0] == "vars" and (len(path) < 2 or path[1] not in known):
                            raise ValueError(
                                f"step '{node_id}': condition uses '{path[1] if len(path) > 1 else 'vars'}', which no step sets"
                            )
        return self


def successors(node: Any) -> list[str | None]:
    if isinstance(node, (CollectNode, ActionNode, LlmNode, KnowledgeNode, MessageNode)):
        return [node.next]
    if isinstance(node, BranchNode):
        return [c.next for c in node.cases] + [node.otherwise]
    if isinstance(node, ConfirmNode):
        return [node.yes, node.no]
    return []


def assigned_vars(wf: WorkflowDef) -> set[str]:
    out = {"last_error"}  # set by an action node's on_error route
    for node in wf.nodes.values():
        if isinstance(node, CollectNode):
            out.update(f.name for f in node.fields)
        elif isinstance(node, (ActionNode, LlmNode, KnowledgeNode)) and node.save_as:
            out.add(node.save_as)
    return out


def used_sources(node: Any) -> set[str]:
    """Every `{{ path }}` a step reads (action args, messages, prompts, summaries...)."""
    if isinstance(node, ActionNode):
        return placeholders(node.args)
    if isinstance(node, LlmNode):
        return placeholders(node.prompt)
    if isinstance(node, KnowledgeNode):
        return placeholders(node.question)
    if isinstance(node, MessageNode):
        return placeholders(node.text)
    if isinstance(node, ConfirmNode):
        return placeholders(node.summary)
    if isinstance(node, HandoffNode):
        return placeholders(node.reason) | placeholders(node.summary)
    if isinstance(node, EndNode):
        return placeholders(node.message or "")
    return set()


def check_against_actions(wf: WorkflowDef, actions: dict[str, ActionDef]) -> None:
    """Cross-check action steps with the agent's actions. Raises ValueError with a readable reason."""
    for node_id, node in wf.nodes.items():
        if not isinstance(node, ActionNode):
            continue
        action = actions.get(node.action)
        where = f"workflow '{wf.name}' step '{node_id}'"
        if action is None:
            raise ValueError(f"{where}: the action '{node.action}' does not exist")
        unknown = sorted(set(node.args) - set(action.parameters))
        if unknown:
            raise ValueError(f"{where}: '{unknown[0]}' is not a parameter of {node.action}")
        missing = sorted(n for n, p in action.parameters.items() if p.required and n not in node.args)
        if missing:
            raise ValueError(f"{where}: {node.action} needs '{missing[0]}'")
