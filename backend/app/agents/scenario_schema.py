"""Scenario tests: a scripted conversation plus assertions about what the agent *does*.

Scenarios assert behaviour, not wording: which actions were called (and with what), which were
not, the final status, and whether the reply mentions or cites something. Actions never touch the
company's real systems; they answer from the agent's sample responses unless the scenario
overrides them (`mocks`), for instance to simulate an order that is out of the return window.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STATUSES = (
    "completed",
    "needs_input",
    "needs_confirmation",
    "pending_approval",
    "action_required",
    "handed_off",
    "failed",
)
MAX_STEPS = 12
MAX_SCENARIOS_PER_AGENT = 50


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MockResponse(_Strict):
    """What a simulated action returns: data, or an error message."""

    result: Any = None
    error: str | None = Field(default=None, max_length=300)


class MockSpec(MockResponse):
    # Different answers for the 1st, 2nd... call (the last one repeats).
    sequence: list[MockResponse] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def _one_form(self) -> "MockSpec":
        if self.sequence is not None and (self.result is not None or self.error is not None):
            raise ValueError("use either result/error or a sequence, not both")
        return self


class ActionCall(_Strict):
    name: str = Field(max_length=40)
    # Every listed argument must match; others are ignored.
    arguments: dict[str, Any] = Field(default_factory=dict)


class Expect(_Strict):
    status: Literal[STATUSES] | None = None
    outcome: str | None = Field(default=None, max_length=40)
    reply_contains: list[str] = Field(default_factory=list, max_length=10)
    reply_not_contains: list[str] = Field(default_factory=list, max_length=10)
    cites: bool | None = None
    actions_called: list[ActionCall] = Field(default_factory=list, max_length=10)
    actions_not_called: list[str] = Field(default_factory=list, max_length=20)
    workflow_started: str | None = Field(default=None, max_length=40)


class Step(_Strict):
    say: str | None = Field(default=None, min_length=1, max_length=2000)
    # Press the Yes/No button on a pending confirmation instead of typing.
    confirm: Literal["yes", "no"] | None = None
    expect: Expect = Field(default_factory=Expect)

    @model_validator(mode="after")
    def _one_action(self) -> "Step":
        if (self.say is None) == (self.confirm is None):
            raise ValueError("each step either says something or presses yes/no")
        return self


class ScenarioEndUser(_Strict):
    id: str = Field(default="scenario-user", min_length=1, max_length=100)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def _small(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(str(value)) > 4096:
            raise ValueError("attributes are too large")
        return value


class ScenarioDef(_Strict):
    description: str = Field(default="", max_length=500)
    end_user: ScenarioEndUser = Field(default_factory=ScenarioEndUser)
    workflow: str | None = Field(default=None, max_length=40)  # start this process on the first message
    mocks: dict[str, MockSpec] = Field(default_factory=dict, max_length=30)
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)


def parse_scenario(raw: dict[str, Any]) -> ScenarioDef:
    return ScenarioDef.model_validate(raw)
