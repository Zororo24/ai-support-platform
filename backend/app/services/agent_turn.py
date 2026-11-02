"""Glue between an authenticated caller (public API or dashboard playground) and the runtime."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.agents.config import AgentConfig
from app.agents.runtime import TurnContext, run_confirmation, run_tool_result, run_turn
from app.db.models import Agent, AgentSession, Conversation
from app.services import sessions as sessions_service


def open_conversation(
    db: Session,
    *,
    agent: Agent,
    external_session_id: str,
    end_user_ref: str,
    end_user_attrs: dict[str, Any],
    source: str,
) -> tuple[AgentSession, Conversation]:
    """Find or create the session and its conversation. Raises SessionOwnerMismatch."""
    session = sessions_service.get_or_create_session(
        db,
        agent=agent,
        external_id=external_session_id,
        end_user_ref=end_user_ref,
        end_user_attrs=end_user_attrs,
    )
    conversation = sessions_service.get_or_create_conversation(db, session=session, source=source)
    return session, conversation


def _context(
    db: Session,
    *,
    agent_id: UUID,
    session_id: UUID,
    conversation_id: UUID,
    config: AgentConfig,
    version: int | None,
    use_mocks: bool,
    mocks: Any = None,
) -> TurnContext:
    """Load the objects in *this* session (the stream path opens its own)."""
    agent = db.get(Agent, agent_id)
    session = db.get(AgentSession, session_id)
    conversation = db.get(Conversation, conversation_id)
    if agent is None or session is None or conversation is None:
        raise LookupError("agent, session or conversation disappeared during the request")
    if not (agent.tenant_id == session.tenant_id == conversation.tenant_id):
        raise LookupError("tenant mismatch")  # defence in depth; cannot happen via the API
    return TurnContext(
        db=db,
        agent=agent,
        config=config,
        version=version,
        session=session,
        conversation=conversation,
        end_user_attrs=dict(session.end_user_attrs or {}),
        use_mocks=use_mocks,
        mocks=mocks,
    )


def execute(
    db: Session,
    *,
    agent_id: UUID,
    session_id: UUID,
    conversation_id: UUID,
    config: AgentConfig,
    version: int | None,
    message: str,
    use_mocks: bool = False,
    workflow: str | None = None,
    mocks: Any = None,
) -> Iterator[dict[str, Any]]:
    ctx = _context(
        db,
        agent_id=agent_id,
        session_id=session_id,
        conversation_id=conversation_id,
        config=config,
        version=version,
        use_mocks=use_mocks,
        mocks=mocks,
    )
    yield from run_turn(ctx, message, workflow)


def confirm(
    db: Session,
    *,
    agent_id: UUID,
    session_id: UUID,
    conversation_id: UUID,
    config: AgentConfig,
    version: int | None,
    confirmation_id: str,
    decision: str,
    use_mocks: bool = False,
    mocks: Any = None,
) -> Iterator[dict[str, Any]]:
    ctx = _context(
        db,
        agent_id=agent_id,
        session_id=session_id,
        conversation_id=conversation_id,
        config=config,
        version=version,
        use_mocks=use_mocks,
        mocks=mocks,
    )
    yield from run_confirmation(ctx, confirmation_id, decision)


def tool_result(
    db: Session,
    *,
    agent_id: UUID,
    session_id: UUID,
    conversation_id: UUID,
    config: AgentConfig,
    version: int | None,
    call_id: str,
    result: Any,
    error: str | None,
    use_mocks: bool = False,
) -> Iterator[dict[str, Any]]:
    ctx = _context(
        db,
        agent_id=agent_id,
        session_id=session_id,
        conversation_id=conversation_id,
        config=config,
        version=version,
        use_mocks=use_mocks,
    )
    yield from run_tool_result(ctx, call_id, result, error)
