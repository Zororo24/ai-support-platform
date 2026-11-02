"""Scenario tests: store them, play them against the draft or a version, record them from a
conversation, and replay a past production run against a new configuration.

Playing uses the real runtime (decision loop, workflows, guards, confirmations) with every action
answered from sample responses, so nothing here can reach the company's systems or send events.
"""

import time
import uuid
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agents.config import AgentConfig, parse_config
from app.agents.mocks import MockSet, mockset_from_recorded
from app.agents.runtime import ConfirmationNotFound, UnknownWorkflow
from app.agents.scenario_schema import (
    MAX_SCENARIOS_PER_AGENT,
    MAX_STEPS,
    ActionCall,
    Expect,
    ScenarioDef,
    ScenarioEndUser,
    parse_scenario,
)
from app.core.llm import LLMUnavailable
from app.db.models import (
    Agent,
    AgentScenario,
    AgentSession,
    Conversation,
    Message,
    MessageRole,
    Run,
    RunStep,
    ScenarioRun,
    User,
)
from app.services import agent_turn
from app.services import agents as agents_service

KEEP_RUNS_PER_AGENT = 30
OUTCOME_STATUS = {
    "needs_confirmation": "needs_confirmation",
    "handoff": "handed_off",
    "pending_approval": "pending_approval",
}  # noqa: E501


class ScenarioError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


# ---- storing scenarios ----------------------------


def _parse(raw: dict[str, Any]) -> ScenarioDef:
    try:
        return parse_scenario(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise ScenarioError(f"{where}: {first['msg']}" if where else first["msg"]) from exc


def list_scenarios(db: Session, *, agent: Agent) -> list[AgentScenario]:
    return list(
        db.scalars(
            select(AgentScenario)
            .where(AgentScenario.agent_id == agent.id, AgentScenario.tenant_id == agent.tenant_id)
            .order_by(AgentScenario.created_at)
        )
    )


def get_scenario(db: Session, *, agent: Agent, scenario_id: UUID) -> AgentScenario | None:
    row = db.get(AgentScenario, scenario_id)
    return row if row is not None and row.agent_id == agent.id and row.tenant_id == agent.tenant_id else None


def _name_taken(db: Session, agent: Agent, name: str, except_id: UUID | None = None) -> bool:
    stmt = select(AgentScenario.id).where(AgentScenario.agent_id == agent.id, AgentScenario.name == name)
    if except_id is not None:
        stmt = stmt.where(AgentScenario.id != except_id)
    return db.scalar(stmt) is not None


def create_scenario(
    db: Session, *, agent: Agent, user: User | None, name: str, enabled: bool, definition: dict[str, Any]
) -> AgentScenario:
    name = name.strip()
    if not name:
        raise ScenarioError("Give the test a name.")
    parsed = _parse(definition)
    count = db.scalar(
        select(func.count()).select_from(AgentScenario).where(AgentScenario.agent_id == agent.id)
    )
    if (count or 0) >= MAX_SCENARIOS_PER_AGENT:
        raise ScenarioError(f"An agent can have at most {MAX_SCENARIOS_PER_AGENT} tests.", 409)
    if _name_taken(db, agent, name):
        raise ScenarioError("This agent already has a test with that name.", 409)
    row = AgentScenario(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        name=name[:120],
        enabled=enabled,
        definition=parsed.model_dump(mode="json"),
        created_by=user.id if user else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_scenario(
    db: Session,
    *,
    agent: Agent,
    row: AgentScenario,
    name: str | None,
    enabled: bool | None,
    definition: dict[str, Any] | None,
) -> AgentScenario:
    if name is not None:
        name = name.strip()
        if not name:
            raise ScenarioError("Give the test a name.")
        if _name_taken(db, agent, name, row.id):
            raise ScenarioError("This agent already has a test with that name.", 409)
        row.name = name[:120]
    if enabled is not None:
        row.enabled = enabled
    if definition is not None:
        row.definition = _parse(definition).model_dump(mode="json")
    db.commit()
    db.refresh(row)
    return row


def delete_scenario(db: Session, *, row: AgentScenario) -> None:
    db.delete(row)
    db.commit()


# ---- playing a conversation ----------------------------


class _StepError(Exception):
    """A step could not run at all (not an assertion failure)."""


class Player:
    """Plays customer messages into a fresh test session of one agent configuration."""

    def __init__(
        self,
        db: Session,
        *,
        agent: Agent,
        config: AgentConfig,
        version: int | None,
        end_user: ScenarioEndUser,
        mocks: MockSet,
        workflow: str | None = None,
    ) -> None:
        self.db, self.agent, self.config, self.version = db, agent, config, version
        self.mocks, self.workflow = mocks, workflow
        self.session, self.conversation = agent_turn.open_conversation(
            db,
            agent=agent,
            external_session_id="test_" + uuid.uuid4().hex[:12],
            end_user_ref=end_user.id,
            end_user_attrs=dict(end_user.attributes),
            source="test",
        )
        self.pending_confirmation: str | None = None
        self._seen: dict[str, int] = {}
        self._first = True

    def _turn(self, events_factory) -> dict[str, Any]:
        try:
            events = list(events_factory())
        except UnknownWorkflow as exc:
            self.db.rollback()
            raise _StepError(f"This agent has no process called “{exc}”.") from exc
        except ConfirmationNotFound as exc:
            self.db.rollback()
            raise _StepError("There was nothing waiting to be confirmed at this point.") from exc
        except LLMUnavailable as exc:
            self.db.rollback()
            raise _StepError("The AI model was unavailable, so this test could not run.") from exc
        except Exception as exc:
            self.db.rollback()
            raise _StepError(f"The conversation failed unexpectedly ({type(exc).__name__}).") from exc
        result = events[-1]["result"]
        pending = result.get("pending_confirmation")
        self.pending_confirmation = pending["id"] if pending else None
        calls, workflow_started = self._new_steps(result["run_id"])
        return {"result": result, "calls": calls, "workflow_started": workflow_started}

    def _new_steps(self, run_id: str | None) -> tuple[list[dict[str, Any]], str | None]:
        if not run_id:
            return [], None
        rows = self.db.scalars(
            select(RunStep)
            .where(RunStep.run_id == UUID(run_id), RunStep.seq > self._seen.get(run_id, 0))
            .order_by(RunStep.seq)
        ).all()
        if rows:
            self._seen[run_id] = rows[-1].seq
        calls = [
            {"name": s.name, "arguments": (s.input or {}).get("arguments", {}), "ok": s.status == "ok"}
            for s in rows
            if s.type == "action"
        ]
        started = next(
            ((s.output or {}).get("workflow") for s in rows if s.type == "workflow" and s.name == "start"),
            None,
        )
        return calls, started

    def say(self, text: str) -> dict[str, Any]:
        workflow = self.workflow if self._first else None
        self._first = False
        return self._turn(
            lambda: agent_turn.execute(
                self.db,
                agent_id=self.agent.id,
                session_id=self.session.id,
                conversation_id=self.conversation.id,
                config=self.config,
                version=self.version,
                message=text,
                use_mocks=True,
                workflow=workflow,
                mocks=self.mocks,
            )
        )

    def confirm(self, yes: bool) -> dict[str, Any]:
        self._first = False
        if self.pending_confirmation is None:
            raise _StepError("There was nothing waiting to be confirmed at this point.")
        return self._turn(
            lambda: agent_turn.confirm(
                self.db,
                agent_id=self.agent.id,
                session_id=self.session.id,
                conversation_id=self.conversation.id,
                config=self.config,
                version=self.version,
                confirmation_id=self.pending_confirmation,
                decision="confirm" if yes else "cancel",
                use_mocks=True,
                mocks=self.mocks,
            )
        )


# ---- checking expectations ----------------------------


def _same(actual: Any, expected: Any) -> bool:
    return actual == expected or str(actual) == str(expected)


def _describe(call: ActionCall) -> str:
    return call.name if not call.arguments else f"{call.name} with {call.arguments}"


def check(expect: Expect, played: dict[str, Any]) -> list[str]:
    """Why this step did not behave as expected (empty when it did)."""
    result, calls = played["result"], played["calls"]
    reply = result["reply"]
    failures: list[str] = []
    if expect.status is not None and result["status"] != expect.status:
        failures.append(f"Expected the status to be “{expect.status}” but it was “{result['status']}”.")
    if expect.outcome is not None and result["outcome"] != expect.outcome:
        failures.append(f"Expected the outcome to be “{expect.outcome}” but it was “{result['outcome']}”.")
    for needle in expect.reply_contains:
        if needle.lower() not in reply.lower():
            failures.append(f"Expected the reply to mention “{needle}”.")
    for needle in expect.reply_not_contains:
        if needle.lower() in reply.lower():
            failures.append(f"The reply should not mention “{needle}”, but it does.")
    if expect.cites is not None and bool(result["citations"]) != expect.cites:
        failures.append(
            "Expected the reply to cite a document."
            if expect.cites
            else "The reply should not cite a document."
        )  # noqa: E501
    for wanted in expect.actions_called:
        matching = [c for c in calls if c["name"] == wanted.name]
        if not matching:
            failures.append(f"Expected {_describe(wanted)} to be called, but it was not.")
        elif wanted.arguments and not any(
            all(k in c["arguments"] and _same(c["arguments"][k], v) for k, v in wanted.arguments.items())
            for c in matching
        ):
            actual = "; ".join(str(c["arguments"]) for c in matching)
            failures.append(f"Expected {_describe(wanted)}, but it was called with {actual}.")
    for name in expect.actions_not_called:
        if any(c["name"] == name for c in calls):
            failures.append(f"{name} should not have been called, but it was.")
    if expect.workflow_started is not None and played["workflow_started"] != expect.workflow_started:
        got = played["workflow_started"] or "none"
        failures.append(f"Expected the process “{expect.workflow_started}” to start, but it was {got}.")
    return failures


def _step_record(
    index: int, step_def: Any, played: dict[str, Any] | None, failures: list[str], error: str | None
):
    entry: dict[str, Any] = {
        "index": index,
        "say": step_def.say,
        "confirm": step_def.confirm,
        "failures": failures,
        "error": error,
    }
    if played is not None:
        result = played["result"]
        entry.update(
            status=result["status"],
            outcome=result["outcome"],
            reply=result["reply"][:2000],
            actions_called=played["calls"],
            run_id=result["run_id"],
        )
    return entry


def run_scenario(
    db: Session, *, agent: Agent, row: AgentScenario, config: AgentConfig, version: int | None
) -> dict[str, Any]:
    started = time.perf_counter()
    defn = parse_scenario(row.definition)
    outcome: dict[str, Any] = {"scenario_id": str(row.id), "name": row.name, "steps": [], "error": None}
    try:
        player = Player(
            db,
            agent=agent,
            config=config,
            version=version,
            end_user=defn.end_user,
            mocks=MockSet(defn.mocks),
            workflow=defn.workflow,
        )
        for index, step in enumerate(defn.steps):
            try:
                played = (
                    player.say(step.say) if step.say is not None else player.confirm(step.confirm == "yes")
                )
            except _StepError as exc:
                outcome["steps"].append(_step_record(index, step, None, [], str(exc)))
                outcome["error"] = str(exc)
                break
            outcome["steps"].append(_step_record(index, step, played, check(step.expect, played), None))
    except Exception as exc:  # opening the session failed; never take the whole run down
        db.rollback()
        outcome["error"] = f"The test could not start ({type(exc).__name__})."
    outcome["passed"] = outcome["error"] is None and all(not s["failures"] for s in outcome["steps"])
    outcome["duration_ms"] = round((time.perf_counter() - started) * 1000)
    return outcome


def resolve_target(db: Session, *, agent: Agent, target: str) -> tuple[AgentConfig, int | None, str]:
    """`draft` or a version number -> (config, version, label)."""
    if target == "draft":
        try:
            return parse_config(agent.draft_config), None, "draft"
        except Exception as exc:
            raise ScenarioError("The draft is invalid; fix it before running tests.") from exc
    try:
        number = int(target.lstrip("v"))
    except ValueError as exc:
        raise ScenarioError("Choose “draft” or a version number.") from exc
    found = agents_service.get_version(db, agent=agent, version=number)
    if found is None:
        raise ScenarioError("That version does not exist.", 404)
    return parse_config(found.config), found.version, f"v{found.version}"


def run_all(
    db: Session,
    *,
    agent: Agent,
    user: User | None,
    target: str,
    purpose: str = "manual",
    scenario_ids: list[UUID] | None = None,
    config: AgentConfig | None = None,
) -> ScenarioRun:
    """Play every enabled scenario (or the chosen ones) and keep the results."""
    if config is None:
        config, version, label = resolve_target(db, agent=agent, target=target)
    else:  # the caller already holds the exact config (the publish gate)
        version, label = None, target
    rows = [r for r in list_scenarios(db, agent=agent) if r.enabled or scenario_ids]
    if scenario_ids:
        rows = [r for r in rows if r.id in set(scenario_ids)]
    results = [run_scenario(db, agent=agent, row=r, config=config, version=version) for r in rows]
    run = ScenarioRun(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        target=label,
        purpose=purpose,
        passed=sum(1 for r in results if r["passed"]),
        failed=sum(1 for r in results if not r["passed"]),
        results=results,
        created_by=user.id if user else None,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    _prune_history(db, agent)
    return run


def _prune_history(db: Session, agent: Agent) -> None:
    keep = (
        select(ScenarioRun.id)
        .where(ScenarioRun.agent_id == agent.id)
        .order_by(ScenarioRun.created_at.desc())
        .limit(KEEP_RUNS_PER_AGENT)
    )  # noqa: E501
    db.execute(delete(ScenarioRun).where(ScenarioRun.agent_id == agent.id, ScenarioRun.id.not_in(keep)))
    db.commit()


def list_runs(db: Session, *, agent: Agent, limit: int = 20) -> list[ScenarioRun]:
    return list(
        db.scalars(
            select(ScenarioRun)
            .where(ScenarioRun.agent_id == agent.id, ScenarioRun.tenant_id == agent.tenant_id)
            .order_by(ScenarioRun.created_at.desc())
            .limit(limit)
        )
    )


def get_run(db: Session, *, agent: Agent, run_id: UUID) -> ScenarioRun | None:
    row = db.get(ScenarioRun, run_id)
    return row if row is not None and row.agent_id == agent.id and row.tenant_id == agent.tenant_id else None


# ---- recording a scenario from a conversation ----------------------------


def _status_of(meta: dict[str, Any]) -> str:
    return meta.get("status") or OUTCOME_STATUS.get(meta.get("outcome", ""), "completed")


def _turn_pairs(db: Session, conversation: Conversation, until=None) -> list[tuple[Message, Message | None]]:
    stmt = select(Message).where(
        Message.conversation_id == conversation.id,
        Message.tenant_id == conversation.tenant_id,
        Message.role.in_((MessageRole.user, MessageRole.assistant)),
    )
    if until is not None:
        stmt = stmt.where(Message.created_at <= until)
    messages = list(db.scalars(stmt.order_by(Message.created_at)))
    pairs: list[tuple[Message, Message | None]] = []
    for i, message in enumerate(messages):
        if message.role == MessageRole.user:
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            pairs.append((message, nxt if nxt is not None and nxt.role == MessageRole.assistant else None))
    return pairs


def record_from_conversation(
    db: Session, *, agent: Agent, user: User, session_id: str, name: str
) -> AgentScenario:
    """Turn a playground conversation into a scenario that asserts what happened."""
    from app.services import sessions as sessions_service

    session = sessions_service.get_session(db, agent=agent, external_id=session_id)
    if session is None or session.end_user_ref != f"staff:{user.id}":
        raise ScenarioError("That conversation was not found.", 404)
    conversation = db.scalars(select(Conversation).where(Conversation.session_id == session.id)).first()
    pairs = _turn_pairs(db, conversation) if conversation else []
    if not pairs:
        raise ScenarioError("That conversation has no messages to record.")
    if len(pairs) > MAX_STEPS:
        raise ScenarioError(f"Only conversations of up to {MAX_STEPS} messages can be recorded.")

    steps: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {}  # actions already reported for a run that spans several messages
    for question, answer in pairs:
        expect: dict[str, Any] = {}
        if answer is not None:
            meta = answer.meta or {}
            expect["status"] = _status_of(meta)
            already = seen.setdefault(str(meta.get("run_id")), set())
            names = [n for n in dict.fromkeys(meta.get("actions", [])) if n not in already]
            already.update(names)
            expect["actions_called"] = [{"name": n} for n in names]
        steps.append({"say": question.content, "expect": expect})
    definition = {
        "description": "Recorded from the playground. Edit the expectations to say what really matters.",
        "end_user": {"id": "recorded-user", "attributes": dict(session.end_user_attrs or {})},
        "steps": steps,
    }
    return create_scenario(db, agent=agent, user=user, name=name, enabled=True, definition=definition)


# ---- replaying a production run ----------------------------


def replay_run(db: Session, *, tenant_id: UUID, run_id: UUID, target: str) -> dict[str, Any]:
    """Re-run a past conversation against another configuration, answering actions with what they
    returned the first time, and report where the behaviour differs."""
    run = db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id:
        raise ScenarioError("Run not found.", 404)
    agent = db.get(Agent, run.agent_id)
    session = db.get(AgentSession, run.session_id)
    conversation = db.get(Conversation, run.conversation_id) if run.conversation_id else None
    if agent is None or session is None or conversation is None:
        raise ScenarioError("The conversation for this run no longer exists.", 404)
    until = run.ended_at or db.scalar(select(func.now()))
    pairs = _turn_pairs(db, conversation, until=until)
    if not pairs:
        raise ScenarioError("This conversation's transcript was not stored, so it cannot be replayed.")
    truncated = len(pairs) > MAX_STEPS
    pairs = pairs[-MAX_STEPS:]

    recorded: dict[str, list[dict[str, Any]]] = {}
    action_steps = db.scalars(
        select(RunStep)
        .join(Run, Run.id == RunStep.run_id)
        .where(Run.conversation_id == conversation.id, RunStep.type == "action", RunStep.created_at <= until)
        .order_by(RunStep.created_at)
    )
    for step in action_steps:
        if (step.input or {}).get("mock") is True:
            continue
        recorded.setdefault(step.name, []).append(step.output or {})

    config, version, label = resolve_target(db, agent=agent, target=target)
    player = Player(
        db,
        agent=agent,
        config=config,
        version=version,
        end_user=ScenarioEndUser(id=session.end_user_ref, attributes=dict(session.end_user_attrs or {})),
        mocks=mockset_from_recorded(recorded),
    )
    turns: list[dict[str, Any]] = []
    for question, answer in pairs:
        meta = (answer.meta or {}) if answer is not None else {}
        original = {
            "status": _status_of(meta) if answer is not None else None,
            "reply": answer.content if answer is not None else None,
            "actions": list(dict.fromkeys(meta.get("actions", []))),
        }
        try:
            played = player.say(question.content)
        except _StepError as exc:
            turns.append(
                {
                    "say": question.content,
                    "original": original,
                    "replayed": None,
                    "error": str(exc),
                    "differs": True,
                }
            )  # noqa: E501
            break
        result = played["result"]
        replayed = {
            "status": result["status"],
            "reply": result["reply"],
            "actions": list(dict.fromkeys(c["name"] for c in played["calls"])),
        }
        differs = original["status"] != replayed["status"] or set(original["actions"]) != set(
            replayed["actions"]
        )
        turns.append(
            {
                "say": question.content,
                "original": original,
                "replayed": replayed,
                "error": None,
                "differs": differs,
            }
        )  # noqa: E501
    return {
        "run_id": str(run.id),
        "target": label,
        "agent_version": run.agent_version,
        "truncated": truncated,
        "differs": any(t["differs"] for t in turns),
        "turns": turns,
    }
