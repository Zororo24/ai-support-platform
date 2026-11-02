import json
import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.schemas.chat import ChatMessageIn, ChatMessageOut
from app.core.llm import LLMUnavailable
from app.core.ratelimit import rate_limited
from app.db.models import MessageRole, User, UserRole
from app.db.session import get_db, get_session_factory
from app.services import chat as chat_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

TITLE_CHARS = 60


class ConversationOut(BaseModel):
    id: UUID
    title: str
    updated_at: datetime


class StoredMessageOut(BaseModel):
    id: UUID
    role: MessageRole
    content: str
    citations: Any = None
    ticket: Any = None
    order: Any = None
    feedback: int | None = None
    meta: Any = None  # admins only
    created_at: datetime


class FeedbackIn(BaseModel):
    value: Literal[1, -1] | None  # null clears the rating


class FeedbackOut(BaseModel):
    message_id: UUID
    feedback: int | None


@router.post("/chat/messages", response_model=ChatMessageOut)
def post_message(
    body: ChatMessageIn,
    user: User = Depends(
        rate_limited("chat", per_user="chat_rate_per_user", per_tenant="chat_rate_per_tenant")
    ),
    db: Session = Depends(get_db),
) -> ChatMessageOut:
    try:
        return chat_service.handle_message(
            db, user=user, content=body.content, conversation_id=body.conversation_id
        )
    except chat_service.ConversationNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    except LLMUnavailable:
        logger.exception("chat failed: LLM unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=UNAVAILABLE_MESSAGE,
        )


UNAVAILABLE_MESSAGE = "The assistant is temporarily unavailable. Please try again in a moment."


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/chat/messages/stream")
def post_message_stream(
    body: ChatMessageIn,
    user: User = Depends(
        rate_limited("chat", per_user="chat_rate_per_user", per_tenant="chat_rate_per_tenant")
    ),
) -> StreamingResponse:
    """Server-sent events: one `stage` event per finished pipeline step, then `done` (or `error`).

    The answer itself arrives whole in `done`: it is generated as a structured object that
    carries its own grounding verdict, so it cannot be released word by word before it is checked.
    """
    user_id = user.id  # the request-scoped session may be closed before the stream ends

    def events():
        with get_session_factory()() as db:
            current = db.get(User, user_id)
            try:
                for event in chat_service.run_message(
                    db, user=current, content=body.content, conversation_id=body.conversation_id
                ):
                    if event["type"] == "stage":
                        yield _sse("stage", {"node": event["node"], "label": event["label"]})
                    else:
                        yield _sse("done", event["result"].model_dump(mode="json"))
            except chat_service.ConversationNotFound:
                yield _sse("error", {"status": 404, "detail": "Conversation not found"})
            except LLMUnavailable:
                logger.exception("chat stream failed: LLM unavailable")
                yield _sse("error", {"status": 503, "detail": UNAVAILABLE_MESSAGE})
            except Exception:
                logger.exception("chat stream failed")
                yield _sse("error", {"status": 500, "detail": "Something went wrong. Please try again."})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[ConversationOut]:
    out = []
    for conversation, first_message in chat_service.list_conversations(db, user=user):
        title = " ".join((first_message or "New conversation").split())
        if len(title) > TITLE_CHARS:
            title = title[:TITLE_CHARS].rstrip() + "…"
        out.append(ConversationOut(id=conversation.id, title=title, updated_at=conversation.updated_at))
    return out


@router.get("/conversations/{conversation_id}/messages", response_model=list[StoredMessageOut])
def list_messages(
    conversation_id: UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[StoredMessageOut]:
    try:
        messages = chat_service.list_messages(db, user=user, conversation_id=conversation_id)
    except chat_service.ConversationNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return [
        StoredMessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            citations=m.citations,
            ticket=(m.payload or {}).get("ticket"),
            order=(m.payload or {}).get("order"),
            feedback=m.feedback,
            meta=m.meta if user.role == UserRole.admin else None,
            created_at=m.created_at,
        )
        for m in messages
    ]


@router.put("/messages/{message_id}/feedback", response_model=FeedbackOut)
def put_feedback(
    message_id: UUID,
    body: FeedbackIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FeedbackOut:
    try:
        message = chat_service.set_feedback(db, user=user, message_id=message_id, value=body.value)
    except chat_service.MessageNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return FeedbackOut(message_id=message.id, feedback=message.feedback)
