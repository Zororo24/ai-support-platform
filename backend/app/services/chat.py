import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.graph import get_graph
from app.api.schemas.chat import ChatMessageOut
from app.api.schemas.orders import OrderOut
from app.core.llm import bind_usage
from app.db.models import Conversation, Message, MessageRole, User, UserRole
from app.services import tenant_llm
from app.services import tickets as tickets_service


class ConversationNotFound(Exception):
    pass


class MessageNotFound(Exception):
    pass


def get_or_create_conversation(db: Session, *, user: User, conversation_id: UUID | None) -> Conversation:
    if conversation_id is None:
        conversation = Conversation(tenant_id=user.tenant_id, user_id=user.id)
        db.add(conversation)
        db.flush()
        return conversation
    conversation = db.get(Conversation, conversation_id)
    # Threads are private to their owner, not just their tenant.
    if conversation is None or conversation.tenant_id != user.tenant_id or conversation.user_id != user.id:
        raise ConversationNotFound
    return conversation


def list_conversations(db: Session, *, user: User) -> list[tuple[Conversation, str | None]]:
    """Own conversations, newest activity first, each with its first user message as a title."""
    first_message = (
        select(Message.content)
        .where(
            Message.conversation_id == Conversation.id,
            Message.role == MessageRole.user,
        )
        .order_by(Message.created_at)
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    stmt = (
        select(Conversation, first_message)
        .where(Conversation.tenant_id == user.tenant_id, Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
    )
    return [(row[0], row[1]) for row in db.execute(stmt).all()]


def list_messages(db: Session, *, user: User, conversation_id: UUID) -> list[Message]:
    conversation = get_or_create_conversation(db, user=user, conversation_id=conversation_id)
    stmt = (
        select(Message)
        .where(Message.tenant_id == user.tenant_id, Message.conversation_id == conversation.id)
        .order_by(Message.created_at)
    )
    return list(db.scalars(stmt))


def set_feedback(db: Session, *, user: User, message_id: UUID, value: int | None) -> Message:
    message = db.get(Message, message_id)
    if message is None or message.tenant_id != user.tenant_id or message.role != MessageRole.assistant:
        raise MessageNotFound
    conversation = db.get(Conversation, message.conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise MessageNotFound
    message.feedback = value
    db.commit()
    db.refresh(message)
    return message


def save_message(
    db: Session,
    *,
    conversation: Conversation,
    role: MessageRole,
    content: str,
    citations=None,
    payload: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> Message:
    message = Message(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        role=role,
        content=content,
        citations=citations,
        payload=payload,
        meta=meta,
    )
    db.add(message)
    db.flush()
    return message


def _stage_label(node: str, delta: dict[str, Any]) -> str:
    """Human-readable, truthful description of a pipeline step that has just finished."""
    if node == "classify":
        confidence = delta.get("confidence")
        pct = f" ({round(confidence * 100)}% sure)" if isinstance(confidence, (int, float)) else ""
        return f"Understood your message as: {delta.get('intent', 'unknown')}{pct}"
    if node == "retrieve":
        n = len(delta.get("retrieved_chunks") or [])
        cached = " (from cache)" if delta.get("cache_hit") else ""
        return f"Found {n} relevant passage{'s' if n != 1 else ''}{cached}"
    if node == "generate":
        if delta.get("outcome") == "answered":
            return "Wrote an answer from your documents"
        return "No supported answer found in the documents"
    if node == "lookup_order":
        return "Looked up the order"
    if node == "create_ticket":
        return "Opened a support ticket"
    return "Preparing a reply"


def run_message(
    db: Session, *, user: User, content: str, conversation_id: UUID | None
) -> Iterator[dict[str, Any]]:
    """Run one chat turn, yielding {"type": "stage", ...} events and a final {"type": "done"}.

    This generator may be resumed on different threads (when streamed), so LLM usage counters
    are re-bound to the current context after every resume.
    """
    conversation = get_or_create_conversation(db, user=user, conversation_id=conversation_id)
    save_message(db, conversation=conversation, role=MessageRole.user, content=content)

    history = [
        {"role": m.role.value, "content": m.content}
        for m in list_messages(db, user=user, conversation_id=conversation.id)
        if m.role in (MessageRole.user, MessageRole.assistant)
    ][-6:]
    state: dict[str, Any] = {
        "tenant_id": str(user.tenant_id),
        "user_id": str(user.id),
        "messages": history,
    }
    usage = tenant_llm.bind(db, user.tenant_id)
    started = time.perf_counter()
    for update in get_graph().stream(
        state,
        config={"configurable": {"db": db, "user": user, "conversation_id": conversation.id}},
        stream_mode="updates",
    ):
        for node, delta in update.items():
            delta = delta or {}
            state.update(delta)
            yield {"type": "stage", "node": node, "label": _stage_label(node, delta)}
            bind_usage(usage)
    latency_ms = round((time.perf_counter() - started) * 1000)

    answer = state["final_response"]
    citations = state.get("citations") or []
    tool_result = state.get("tool_result") or {}
    ticket = tool_result.get("ticket")
    order = tool_result.get("order")

    ticket_out = tickets_service.to_out(db, [ticket])[0] if ticket else None
    order_out = OrderOut.model_validate(order, from_attributes=True) if order else None

    payload: dict[str, Any] = {}
    if ticket_out:
        payload["ticket"] = ticket_out.model_dump(mode="json")
    if order_out:
        payload["order"] = order_out.model_dump(mode="json")

    meta = {
        "question": content,
        "intent": state.get("intent"),
        "confidence": state.get("confidence"),
        "outcome": state.get("outcome", "other"),
        "cache_hit": state.get("cache_hit", False),
        "latency_ms": latency_ms,
        "llm_calls": usage["calls"],
        "total_tokens": usage["total_tokens"],
        "retrieved": [
            {"document_id": c["document_id"], "title": c["title"], "score": round(c["score"], 4)}
            for c in state.get("retrieved_chunks") or []
        ],
        "cited": [c["document_id"] for c in citations],
    }
    reply = save_message(
        db,
        conversation=conversation,
        role=MessageRole.assistant,
        content=answer,
        citations=citations,
        payload=payload or None,
        meta=meta,
    )
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()

    yield {
        "type": "done",
        "result": ChatMessageOut(
            conversation_id=conversation.id,
            message_id=reply.id,
            content=answer,
            citations=citations,
            ticket=ticket_out,
            order=order_out,
            intent=state.get("intent"),
            cache_hit=state.get("cache_hit", False),
            # Internals (sources considered, latency, tokens) are for admins only.
            meta=meta if user.role == UserRole.admin else {},
        ),
    }


def handle_message(db: Session, *, user: User, content: str, conversation_id: UUID | None) -> ChatMessageOut:
    result: ChatMessageOut | None = None
    for event in run_message(db, user=user, content=content, conversation_id=conversation_id):
        if event["type"] == "done":
            result = event["result"]
    assert result is not None
    return result
