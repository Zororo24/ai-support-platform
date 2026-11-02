from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Agent, AgentSession, Conversation


class SessionOwnerMismatch(Exception):
    """A session id was reused for a different end user."""


def get_or_create_session(
    db: Session,
    *,
    agent: Agent,
    external_id: str,
    end_user_ref: str,
    end_user_attrs: dict[str, Any],
) -> AgentSession:
    def _find() -> AgentSession | None:
        return db.scalars(
            select(AgentSession).where(
                AgentSession.agent_id == agent.id,
                AgentSession.tenant_id == agent.tenant_id,
                AgentSession.external_id == external_id,
            )
        ).first()

    session = _find()
    if session is None:
        session = AgentSession(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            external_id=external_id,
            end_user_ref=end_user_ref,
            end_user_attrs=end_user_attrs,
        )
        db.add(session)
        try:
            db.commit()
        except IntegrityError:  # two first messages raced; use the winner's row
            db.rollback()
            session = _find()
            if session is None:
                raise
    if session.end_user_ref != end_user_ref:
        # Session ids are chosen by the company, but reusing one across customers would mix
        # their conversations, so refuse rather than silently merging.
        raise SessionOwnerMismatch
    session.end_user_attrs = end_user_attrs
    session.last_active_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)
    return session


def get_session(db: Session, *, agent: Agent, external_id: str) -> AgentSession | None:
    return db.scalars(
        select(AgentSession).where(
            AgentSession.agent_id == agent.id,
            AgentSession.tenant_id == agent.tenant_id,
            AgentSession.external_id == external_id,
        )
    ).first()


def get_or_create_conversation(db: Session, *, session: AgentSession, source: str) -> Conversation:
    conversation = db.scalars(
        select(Conversation).where(
            Conversation.session_id == session.id, Conversation.tenant_id == session.tenant_id
        )
    ).first()
    if conversation is None:
        conversation = Conversation(
            tenant_id=session.tenant_id,
            agent_id=session.agent_id,
            session_id=session.id,
            end_user_ref=session.end_user_ref,
            source=source,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    return conversation


def delete_session(db: Session, *, agent: Agent, external_id: str) -> bool:
    session = get_session(db, agent=agent, external_id=external_id)
    if session is None:
        return False
    # Conversations, messages (and run data in later phases) go with it via ON DELETE CASCADE.
    db.execute(delete(AgentSession).where(AgentSession.id == session.id))
    db.commit()
    return True


def delete_end_user(db: Session, *, agent: Agent, end_user_ref: str) -> int:
    result = db.execute(
        delete(AgentSession).where(
            AgentSession.agent_id == agent.id,
            AgentSession.tenant_id == agent.tenant_id,
            AgentSession.end_user_ref == end_user_ref,
        )
    )
    db.commit()
    return result.rowcount or 0


def message_count(db: Session, *, conversation_id: UUID, tenant_id: UUID) -> int:
    from sqlalchemy import func

    from app.db.models import Message

    return (
        db.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id, Message.tenant_id == tenant_id)
        )
        or 0
    )
