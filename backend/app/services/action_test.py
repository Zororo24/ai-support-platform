"""Dry-run of one action from an agent's draft, for the action editor's "Test" button."""

from typing import Any

from sqlalchemy.orm import Session

from app.agents import executor
from app.agents.config import parse_config
from app.db.models import Agent
from app.services import connections as connections_service


class ActionTestError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def run(
    db: Session,
    *,
    agent: Agent,
    action_name: str,
    arguments: dict[str, Any],
    context: dict[str, Any],
    use_mock: bool,
    send_for_real: bool,
) -> dict[str, Any]:
    try:
        config = parse_config(agent.draft_config)
    except Exception as exc:
        raise ActionTestError("The draft is invalid; fix it before testing actions.") from exc
    action = next((a for a in config.actions if a.name == action_name), None)
    if action is None:
        raise ActionTestError("That action is not in the saved draft. Save the draft first.", 404)

    try:
        args = executor.validate_args(action, arguments)
        executor.check_guards(action, args, context)
    except executor.ActionRefused as exc:
        return {
            "refused": {"code": exc.code, "message": exc.message},
            "request": None,
            "result": None,
            "model_view": None,
        }

    if use_mock:
        if action.mock is None:
            raise ActionTestError("This action has no mock response defined.")
        result = executor.ActionResult(ok=True, status_code=200, data=action.mock)
        return {
            "refused": None,
            "request": None,
            "result": {
                "ok": True,
                "status_code": 200,
                "latency_ms": 0,
                "error_code": None,
                "error": None,
                "mock": True,
            },
            "model_view": executor.shape_for_model(action, result),
        }

    if action.execution == "handback":
        raise ActionTestError(
            "Your own backend performs this action, so it can't be run from here. Test with the sample response."
        )
    if action.effect == "write" and not send_for_real:
        raise ActionTestError(
            "This action changes data in the company's system. Tick 'send for real' to run it, or test with the mock."  # noqa: E501
        )
    connection = connections_service.load_info(
        db, tenant_id=agent.tenant_id, connection_id=action.connection_id
    )
    if connection is None:
        raise ActionTestError("The connection for this action is missing or its credentials cannot be read.")
    try:
        rendered = executor.render_request(action, args, context, connection, idempotency_key="test")
    except executor.ActionRefused as exc:
        return {
            "refused": {"code": exc.code, "message": exc.message},
            "request": None,
            "result": None,
            "model_view": None,
        }
    result = executor.call(action, rendered, client=executor.client_override())
    return {
        "refused": None,
        "request": {
            "method": rendered.method,
            "url": rendered.url,
            "query": rendered.params,
            "headers": executor.redact(rendered.headers),
            "body": rendered.json_body,
        },
        "result": {
            "ok": result.ok,
            "status_code": result.status_code,
            "latency_ms": result.latency_ms,
            "error_code": result.error_code,
            "error": result.error,
            "mock": False,
        },
        "model_view": executor.shape_for_model(action, result),
    }
