"""Public API: what a company's backend calls. Authenticated with a secret API key."""

import json
import logging
import secrets
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import ConfirmationNotFound, ToolResultNotFound, ToolResultPending
from app.api.public_deps import ApiPrincipal, get_api_principal
from app.api.schemas.v1 import (
    AgentMessageIn,
    AgentMessageOut,
    ConfirmationIn,
    SessionStateOut,
    ToolResultIn,
)
from app.core import idempotency, ratelimit
from app.core.api_errors import ApiError
from app.core.config import get_settings
from app.core.llm import LLMUnavailable
from app.db.models import Agent, Run
from app.db.session import get_db, get_session_factory
from app.services import agent_turn, budgets
from app.services import agents as agents_service
from app.services import sessions as sessions_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["public api"])

MODEL_UNAVAILABLE = "The assistant is temporarily unavailable. Please retry shortly."


def _rate_limit_headers(limit: int, remaining: int, reset: int) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset),
    }


def _enforce_rate_limits(principal: ApiPrincipal, limits: Any, end_user_ref: str) -> dict[str, str]:
    """Per key, per end user and per tenant. Returns the key's headroom headers."""
    settings = get_settings()
    tenant = principal.tenant_id
    checks = [
        ("api key", f"{tenant}:api:key:{principal.key.id}", limits.requests_per_minute_per_key),
        (
            "end user",
            f"{tenant}:api:{principal.agent.id}:user:{end_user_ref}",
            limits.requests_per_minute_per_end_user,
        ),
        ("workspace", f"{tenant}:api:tenant", settings.api_rate_per_tenant),
    ]
    headers: dict[str, str] = {}
    for index, (name, scope, limit) in enumerate(checks):
        allowed, remaining, reset = ratelimit.hit_detailed(scope, limit=limit, window=60)
        if index == 0:
            headers = _rate_limit_headers(limit, remaining, reset)
        if not allowed:
            raise ApiError(
                429,
                "rate_limited",
                f"Rate limit exceeded for this {name}. Retry in {reset} seconds.",
                {"Retry-After": str(reset), **headers},
            )
    return headers


def _acquire(
    principal: ApiPrincipal, limits: Any, session_ext: str, end_user_ref: str
) -> tuple[dict[str, str], Any]:
    """Apply rate limits, take a concurrency slot and the session lock.

    Returns (rate-limit headers, release function). Raises ApiError and leaves nothing held
    when any step refuses.
    """
    limit_headers = _enforce_rate_limits(principal, limits, end_user_ref)
    slot_scope = str(principal.key.id)
    if not ratelimit.acquire_slot(slot_scope, limit=limits.max_concurrent_requests):
        raise ApiError(
            429, "too_many_concurrent_requests", "Too many requests are running at once for this key.",
            {"Retry-After": "1", **limit_headers},
        )  # fmt: skip
    lock = ratelimit.acquire_lock(f"{principal.agent.id}:{session_ext}")
    if lock is None:
        ratelimit.release_slot(slot_scope)
        raise ApiError(409, "session_busy", "Another message in this session is still being processed.")

    def release() -> None:
        ratelimit.release_lock(lock)
        ratelimit.release_slot(slot_scope)

    return limit_headers, release


_AWAITING_TEXT = (
    "This session is waiting for your system to return the result of an action. "
    "Send it to POST /v1/agents/{id}/tool-results first."
)


def _awaiting_tool_result(call_id: str) -> ApiError:
    return ApiError(409, "awaiting_tool_result", f"{_AWAITING_TEXT} (call_id: {call_id})")


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/agents/{agent_id}/messages", response_model=AgentMessageOut)
def post_message(
    body: AgentMessageIn,
    response: Response,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200),
):
    """Send one end-user message and get the agent's reply (JSON, or SSE with `stream: true`)."""
    agent = principal.agent
    live = agents_service.live_config(db, agent=agent)
    if live is None:
        raise ApiError(409, "agent_not_published", "This agent has no published version yet.")
    config, version = live
    if body.workflow is not None and body.workflow not in {w.name for w in config.workflows if w.enabled}:
        raise ApiError(422, "unknown_workflow", f"This agent has no process called '{body.workflow}'.")

    who = budgets.Subject(agent.tenant_id, agent.id, agent.name)
    try:
        budgets.check(db, agent=agent, config=config)
    except budgets.BudgetExceeded as exc:
        owner = "This agent has" if exc.scope == "agent" else "This workspace has"
        raise ApiError(
            429,
            "budget_exceeded",
            f"{owner} used its daily AI budget. It resets at midnight UTC.",
            {"Retry-After": str(exc.retry_after)},
        )

    session_ext = body.session_id or "s_" + secrets.token_hex(8)
    end_user_ref = body.end_user.id if body.end_user else f"anonymous:{session_ext}"
    attrs = dict(body.end_user.attributes) if body.end_user else {}

    use_idem = bool(idempotency_key) and not body.stream
    if idempotency_key and body.stream:
        raise ApiError(422, "invalid_request", "Idempotency-Key is not supported with stream=true.")
    fp = idempotency.fingerprint(body.model_dump(mode="json")) if use_idem else ""
    if use_idem:
        state, stored = idempotency.begin(principal.tenant_id, principal.key.id, idempotency_key, fp)
        if state == "replay":
            response.headers["Idempotent-Replay"] = "true"
            return stored
        if state == "in_progress":
            raise ApiError(
                409, "request_in_progress", "A request with this Idempotency-Key is still running."
            )
        if state == "conflict":
            raise ApiError(
                422, "idempotency_key_reuse", "This Idempotency-Key was used with a different body."
            )

    def abort_idem() -> None:
        if use_idem:
            idempotency.abort(principal.tenant_id, principal.key.id, idempotency_key)

    try:
        limit_headers, release = _acquire(principal, config.limits, session_ext, end_user_ref)
    except ApiError:
        abort_idem()
        raise

    try:
        session, conversation = agent_turn.open_conversation(
            db,
            agent=agent,
            external_session_id=session_ext,
            end_user_ref=end_user_ref,
            end_user_attrs=attrs,
            source="api",
        )
    except sessions_service.SessionOwnerMismatch:
        release()
        abort_idem()
        raise ApiError(409, "session_user_mismatch", "This session belongs to a different end user.")
    except Exception:
        release()
        abort_idem()
        raise

    turn = dict(
        agent_id=agent.id,
        session_id=session.id,
        conversation_id=conversation.id,
        config=config,
        version=version,
        message=body.message,
        workflow=body.workflow,
    )

    if body.stream:
        return StreamingResponse(
            _stream(turn, session_ext, release, who, config),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **limit_headers},
        )

    try:
        events = list(agent_turn.execute(db, **turn))
        result = events[-1]["result"]
    except ToolResultPending as exc:
        abort_idem()
        raise _awaiting_tool_result(exc.call_id)
    except LLMUnavailable:
        logger.exception("v1 message failed: model unavailable")
        abort_idem()
        raise ApiError(503, "model_unavailable", MODEL_UNAVAILABLE, {"Retry-After": "5"})
    except Exception:
        abort_idem()
        raise
    finally:
        release()

    budgets.after_turn(db, who=who, config=config)
    out = AgentMessageOut(session_id=session_ext, **result)
    if use_idem:
        idempotency.complete(
            principal.tenant_id, principal.key.id, idempotency_key, fp, out.model_dump(mode="json")
        )
    for name, value in limit_headers.items():
        response.headers[name] = value
    return out


def _stream(turn: dict[str, Any], session_ext: str, release, who, config) -> Iterator[str]:
    """SSE body. Owns its DB session (the request's may close first) and releases the
    session lock and concurrency slot when the stream ends, however it ends."""
    try:
        with get_session_factory()() as stream_db:
            try:
                for event in agent_turn.execute(stream_db, **turn):
                    if event["type"] == "stage":
                        yield _sse("stage", {"node": event["node"], "label": event["label"]})
                    else:
                        done = AgentMessageOut(session_id=session_ext, **event["result"])
                        yield _sse("done", done.model_dump(mode="json"))
                        budgets.after_turn(stream_db, who=who, config=config)
            except ToolResultPending as exc:
                yield _sse(
                    "error",
                    {"code": "awaiting_tool_result", "message": _AWAITING_TEXT, "call_id": exc.call_id},
                )
            except LLMUnavailable:
                logger.exception("v1 stream failed: model unavailable")
                yield _sse("error", {"code": "model_unavailable", "message": MODEL_UNAVAILABLE})
            except Exception:
                logger.exception("v1 stream failed")
                yield _sse("error", {"code": "internal_error", "message": "Something went wrong."})
    finally:
        release()


@router.post("/agents/{agent_id}/confirmations", response_model=AgentMessageOut)
def post_confirmation(
    body: ConfirmationIn,
    response: Response,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: Session = Depends(get_db),
):
    """Approve or cancel the action a session is waiting on (for UIs with Yes/No buttons).

    Free-text "yes"/"no" sent to the messages endpoint works too; this is the deterministic route.
    """
    agent = principal.agent
    live = agents_service.live_config(db, agent=agent)
    if live is None:
        raise ApiError(409, "agent_not_published", "This agent has no published version yet.")
    config, version = live
    session = sessions_service.get_session(db, agent=agent, external_id=body.session_id)
    if session is None:
        raise ApiError(404, "session_not_found", "No such session.")
    limit_headers, release = _acquire(principal, config.limits, body.session_id, session.end_user_ref)
    try:
        conversation = sessions_service.get_or_create_conversation(db, session=session, source="api")
        events = list(
            agent_turn.confirm(
                db,
                agent_id=agent.id,
                session_id=session.id,
                conversation_id=conversation.id,
                config=config,
                version=version,
                confirmation_id=body.confirmation_id,
                decision=body.decision,
            )
        )
    except ConfirmationNotFound:
        raise ApiError(
            404,
            "confirmation_not_found",
            "Nothing is waiting for that confirmation. It may have expired or already been handled.",
        )
    except LLMUnavailable:
        logger.exception("v1 confirmation failed: model unavailable")
        raise ApiError(503, "model_unavailable", MODEL_UNAVAILABLE, {"Retry-After": "5"})
    finally:
        release()
    for name, value in limit_headers.items():
        response.headers[name] = value
    return AgentMessageOut(session_id=body.session_id, **events[-1]["result"])


@router.post("/agents/{agent_id}/tool-results", response_model=AgentMessageOut)
def post_tool_result(
    body: ToolResultIn,
    response: Response,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: Session = Depends(get_db),
):
    """Return the outcome of an action the agent asked your backend to perform (`action_request`).

    The agent then continues from your result and answers; the response has the same shape as a
    message response.
    """
    agent = principal.agent
    live = agents_service.live_config(db, agent=agent)
    if live is None:
        raise ApiError(409, "agent_not_published", "This agent has no published version yet.")
    config, version = live
    session = _require_session(db, agent, body.session_id)
    limit_headers, release = _acquire(principal, config.limits, body.session_id, session.end_user_ref)
    try:
        conversation = sessions_service.get_or_create_conversation(db, session=session, source="api")
        events = list(
            agent_turn.tool_result(
                db,
                agent_id=agent.id,
                session_id=session.id,
                conversation_id=conversation.id,
                config=config,
                version=version,
                call_id=body.call_id,
                result=body.result,
                error=body.error,
            )
        )
    except ToolResultNotFound:
        raise ApiError(
            404,
            "tool_call_not_found",
            "Nothing is waiting for that call. It may have expired or already been answered.",
        )
    except LLMUnavailable:
        logger.exception("v1 tool result failed: model unavailable")
        raise ApiError(503, "model_unavailable", MODEL_UNAVAILABLE, {"Retry-After": "5"})
    finally:
        release()
    for name, value in limit_headers.items():
        response.headers[name] = value
    return AgentMessageOut(session_id=body.session_id, **events[-1]["result"])


def _active_run(db: Session, session) -> dict[str, Any] | None:
    run = db.scalars(
        select(Run)
        .where(
            Run.session_id == session.id,
            Run.tenant_id == session.tenant_id,
            Run.status.in_(("waiting_confirmation", "waiting_tool_result", "waiting_approval")),
        )
        .order_by(Run.started_at.desc())
    ).first()
    if run is None:
        return None
    pending = run.pending or {}
    active: dict[str, Any] = {"run_id": str(run.id), "status": run.status}
    if run.status == "waiting_confirmation":
        active["pending_confirmation"] = {
            k: pending.get(k) for k in ("id", "action", "summary", "args_preview", "expires_at")
        }
    elif run.status == "waiting_tool_result":
        active["action_request"] = {
            "call_id": pending.get("id"),
            "name": pending.get("action"),
            "arguments": pending.get("args"),
            "expires_at": pending.get("expires_at"),
        }
    else:
        active["pending_approval"] = {k: pending.get(k) for k in ("id", "action", "summary", "expires_at")}
    return active


def _require_session(db: Session, agent: Agent, session_id: str):
    session = sessions_service.get_session(db, agent=agent, external_id=session_id)
    if session is None:
        raise ApiError(404, "session_not_found", "No such session.")
    return session


@router.get("/agents/{agent_id}/sessions/{session_id}", response_model=SessionStateOut)
def get_session_state(
    session_id: str,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: Session = Depends(get_db),
) -> SessionStateOut:
    session = _require_session(db, principal.agent, session_id)
    conversation = sessions_service.get_or_create_conversation(db, session=session, source="api")
    return SessionStateOut(
        session_id=session.external_id,
        end_user_id=session.end_user_ref,
        created_at=session.created_at,
        last_active_at=session.last_active_at,
        message_count=sessions_service.message_count(
            db, conversation_id=conversation.id, tenant_id=session.tenant_id
        ),
        active_run=_active_run(db, session),
    )


@router.delete("/agents/{agent_id}/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: Session = Depends(get_db),
) -> Response:
    """Delete a session and everything stored about it (privacy requests)."""
    if not sessions_service.delete_session(db, agent=principal.agent, external_id=session_id):
        raise ApiError(404, "session_not_found", "No such session.")
    return Response(status_code=204)


@router.delete("/agents/{agent_id}/end-users/{end_user_ref}")
def delete_end_user(
    end_user_ref: str,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Delete every session of one end user."""
    return {
        "deleted_sessions": sessions_service.delete_end_user(
            db, agent=principal.agent, end_user_ref=end_user_ref
        )
    }
