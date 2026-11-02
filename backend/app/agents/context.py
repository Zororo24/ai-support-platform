from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.agents.action_schema import ActionDef
from app.agents.config import AgentConfig
from app.agents.workflow_schema import WorkflowDef
from app.db.models import Agent, AgentSession, Conversation


@dataclass
class TurnContext:
    db: Session
    agent: Agent
    config: AgentConfig
    version: int | None  # None while testing the unpublished draft
    session: AgentSession
    conversation: Conversation
    end_user_attrs: dict[str, Any]
    use_mocks: bool = False  # playground only: answer actions from their canned `mock`
    # False when a run continues without a new customer message (tool result, staff approval).
    record_user: bool = True
    # Scenario tests and replays script what actions return (a `MockSet`); None = their sample.
    mocks: Any = None

    @property
    def tenant_id(self):
        return self.agent.tenant_id

    @property
    def end_user_ref(self) -> str:
        return self.session.end_user_ref

    @property
    def context(self) -> dict[str, Any]:
        """Trusted facts about the caller, injected into action requests. `end_user_id` always
        wins over a same-named attribute, so a client cannot spoof it."""
        return {**self.end_user_attrs, "end_user_id": self.session.end_user_ref}

    @property
    def redacts_pii(self) -> bool:
        return self.config.privacy.redact_pii

    @property
    def stores_transcript(self) -> bool:
        return self.config.privacy.store_transcript == "full"

    @property
    def actions(self) -> dict[str, ActionDef]:
        return {a.name: a for a in self.config.actions if a.enabled}

    @property
    def workflows(self) -> dict[str, WorkflowDef]:
        return {w.name: w for w in self.config.workflows if w.enabled}
