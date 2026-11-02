"""The editable configuration of an agent. Stored as JSON; validated here on every save.

Unknown fields are rejected so typos surface as errors instead of silently doing nothing.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.action_schema import ActionDef
from app.agents.workflow_schema import WorkflowDef, check_against_actions


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeConfig(_Strict):
    collection_ids: list[UUID] = Field(default_factory=list, max_length=50)
    top_k: int = Field(default=5, ge=1, le=10)
    min_similarity: float = Field(default=0.1, ge=0.0, le=0.9)
    # Optional quality steps, each one extra model call per search:
    rewrite_queries: bool = False  # make follow-up questions standalone before searching
    rerank: bool = False  # let the model put the best passages first
    # Said when the documents don't cover the question.
    not_found_message: str = Field(
        default="I couldn't find that in our knowledge base. Could you rephrase your question?",
        min_length=1,
        max_length=500,
    )


class LimitsConfig(_Strict):
    max_steps_per_turn: int = Field(default=8, ge=1, le=20)
    run_ttl_hours: int = Field(default=24, ge=1, le=168)
    daily_token_budget: int | None = Field(default=None, ge=1000)
    requests_per_minute_per_key: int = Field(default=60, ge=1, le=6000)
    requests_per_minute_per_end_user: int = Field(default=20, ge=1, le=600)
    max_concurrent_requests: int = Field(default=5, ge=1, le=100)


class PrivacyConfig(_Strict):
    # "none": nothing from the conversation is stored (the session record still is).
    store_transcript: Literal["full", "none"] = "full"
    # Conversations with no activity for this long are deleted by the nightly cleanup.
    retention_days: int = Field(default=30, ge=1, le=365)
    # Mask email addresses, card numbers and phone numbers in what is stored (not in what the model sees).
    redact_pii: bool = False


class HandoffConfig(_Strict):
    """Optional automation when the agent hands a customer to a person: run one of its actions
    (e.g. create a ticket in your help desk) with the handoff reason and summary filled in.

    Use `{{handoff.reason}}`, `{{handoff.summary}}` and `{{context.*}}` in `args`.
    """

    action: str
    args: dict[str, str] = Field(default_factory=dict)

    def validate_against(self, actions: dict[str, ActionDef]) -> None:
        from app.agents.expressions import split_path
        from app.agents.templating import placeholders

        action = actions.get(self.action)
        if action is None:
            raise ValueError(f"handoff: the action '{self.action}' does not exist")
        unknown = sorted(set(self.args) - set(action.parameters))
        if unknown:
            raise ValueError(f"handoff: '{unknown[0]}' is not a parameter of {self.action}")
        missing = sorted(n for n, p in action.parameters.items() if p.required and n not in self.args)
        if missing:
            raise ValueError(f"handoff: {self.action} needs '{missing[0]}'")
        for source in placeholders(self.args):
            segments = split_path(source)
            if segments is None or segments[0] not in ("handoff", "context"):
                raise ValueError(f"handoff: '{source}' must start with handoff. or context.")
            if segments[0] == "handoff" and (len(segments) != 2 or segments[1] not in ("reason", "summary")):
                raise ValueError(f"handoff: '{source}' must be handoff.reason or handoff.summary")


class AgentConfig(_Strict):
    instructions: str = Field(default="", max_length=8000)
    language: str = Field(default="en", max_length=16)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    actions: list[ActionDef] = Field(default_factory=list, max_length=50)
    # End-user attributes the model is allowed to see. Everything else the caller sends is used
    # only to fill in action requests (identity binding) and never shown to the model.
    expose_attributes: list[str] = Field(default_factory=list, max_length=20)
    workflows: list[WorkflowDef] = Field(default_factory=list, max_length=30)
    handoff: HandoffConfig | None = None
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)

    @field_validator("expose_attributes")
    @classmethod
    def _attribute_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not name or len(name) > 60 or not name.replace("_", "").isalnum():
                raise ValueError(f"invalid attribute name '{name}'")
        return value

    @model_validator(mode="after")
    def _unique_action_names(self) -> "AgentConfig":
        names = [a.name for a in self.actions]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"actions: duplicate name '{duplicates[0]}'")
        workflow_names = [w.name for w in self.workflows]
        dup_workflows = sorted({n for n in workflow_names if workflow_names.count(n) > 1})
        if dup_workflows:
            raise ValueError(f"workflows: duplicate name '{dup_workflows[0]}'")
        by_name = {a.name: a for a in self.actions}
        for wf in self.workflows:
            check_against_actions(wf, by_name)
        if self.handoff is not None:
            self.handoff.validate_against(by_name)
        return self


def parse_config(raw: dict[str, Any] | None) -> AgentConfig:
    return AgentConfig.model_validate(raw or {})
