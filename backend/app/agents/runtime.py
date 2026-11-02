"""Runs one turn of an agent for a session.

Three paths, chosen in code (not by the model):
  1. The session has a run waiting for the end user's approval  -> resume it.
  2. The agent has no actions (a pure knowledge bot)             -> retrieve and answer. This
     skips the extra "decide" model call, so it is cheaper and fully deterministic.
  3. Otherwise                                                    -> the decision loop.
All of them yield {"type": "stage", ...} events and finish with {"type": "done", "result": {...}}.
"""

import time
from collections.abc import Iterator
from typing import Any

from app.agents import loop
from app.agents.context import TurnContext
from app.services import tenant_llm

__all__ = [
    "ConfirmationNotFound",
    "ToolResultNotFound",
    "ToolResultPending",
    "TurnContext",
    "UnknownWorkflow",
    "run_confirmation",
    "run_tool_result",
    "run_turn",
]


class ConfirmationNotFound(Exception):
    """There is nothing waiting for approval that matches the request."""


class ToolResultNotFound(Exception):
    """No hand-back action with that call id is waiting for a result."""


class ToolResultPending(Exception):
    """The session is waiting for the company's backend to return an action result."""

    def __init__(self, call_id: str):
        super().__init__(call_id)
        self.call_id = call_id


class UnknownWorkflow(Exception):
    """The requested workflow does not exist or is disabled."""


def run_turn(ctx: TurnContext, user_message: str, workflow: str | None = None) -> Iterator[dict[str, Any]]:
    usage = tenant_llm.bind(ctx.db, ctx.tenant_id)
    started = time.perf_counter()

    if workflow is not None:
        # The company's chatbot chose the process (e.g. the customer pressed "Start a return").
        if workflow not in ctx.workflows:
            raise UnknownWorkflow(workflow)
        stale = loop.waiting_run(ctx)
        if stale is not None:  # starting something new supersedes whatever was waiting
            stale.status, stale.pending, stale.state, stale.ended_at = "cancelled", None, {}, loop.now()
            ctx.db.commit()
        yield from loop.workflow_turn(ctx, workflow, user_message=user_message, usage=usage, started=started)
        return

    waiting = loop.waiting_run(ctx)
    if waiting is not None and waiting.status == loop.WAITING_TOOL:
        raise ToolResultPending((waiting.pending or {}).get("id", ""))
    if waiting is not None:
        yield from loop.resume(
            ctx, waiting, user_message=user_message, decision=None, usage=usage, started=started
        )
    elif not ctx.actions:
        yield from loop.knowledge_turn(ctx, user_message=user_message, usage=usage, started=started)
    else:
        yield from loop.start_turn(ctx, user_message=user_message, usage=usage, started=started)


def run_confirmation(ctx: TurnContext, confirmation_id: str, decision: str) -> Iterator[dict[str, Any]]:
    """Explicit approve/cancel of a pending action, for UIs with Yes/No buttons."""
    usage = tenant_llm.bind(ctx.db, ctx.tenant_id)
    started = time.perf_counter()
    waiting = loop.waiting_run(ctx)
    if (
        waiting is None
        or waiting.status == loop.WAITING_TOOL
        or (waiting.pending or {}).get("id") != confirmation_id
    ):
        raise ConfirmationNotFound
    yield from loop.resume(ctx, waiting, user_message=None, decision=decision, usage=usage, started=started)


def run_tool_result(
    ctx: TurnContext, call_id: str, result: Any, error: str | None
) -> Iterator[dict[str, Any]]:
    """The company's backend performed a hand-back action; continue the run with its outcome."""
    usage = tenant_llm.bind(ctx.db, ctx.tenant_id)
    started = time.perf_counter()
    waiting = loop.waiting_run(ctx)
    if waiting is None or waiting.status != loop.WAITING_TOOL or (waiting.pending or {}).get("id") != call_id:
        raise ToolResultNotFound
    yield from loop.resume_tool(ctx, waiting, result=result, error=error, usage=usage, started=started)
