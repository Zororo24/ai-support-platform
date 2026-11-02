"""Phase K: handoff automation, actions performed by the company's own backend (hand-back), and
staff approvals. The model is scripted; the company's API and webhook receiver are in-process fakes."""

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Approval, Message, MessageRole, Run
from app.db.session import get_session_factory
from app.services import housekeeping
from tests.company import CUSTOMER, make_store_bot, make_workflow_bot, template_workflow
from tests.test_agent_actions import TICKET, reply, run_row, steps
from tests.test_jobs_and_webhooks import make_hook
from tests.test_public_api import error, make_bot

HANDOFF = {"type": "handoff", "reason": "Customer is upset", "summary": "Refund dispute on ACM-1002"}
TICKET_HANDOFF = {"action": "create_ticket", "args": {"subject": "{{handoff.reason}}"}}

REFUND = {
    "name": "issue_refund",
    "description": "Refund an order.",
    "execution": "handback",
    "effect": "write",
    "confirmation": "none",
    "parameters": {
        "order_number": {"type": "string", "required": True},
        "amount_cents": {"type": "integer", "required": True, "minimum": 1},
    },
    "guards": [{"when": "args.amount_cents > 20000", "message": "Refunds over $200 need a person."}],
    "response": {"expose": ["$.refund_id", "$.status"]},
    "mock": {"refund_id": "RF-MOCK", "status": "issued"},
}
REFUND_CALL = {
    "type": "call_action",
    "name": "issue_refund",
    "arguments": {"order_number": "ACM-1002", "amount_cents": 1999},
}


def set_handoff(bot, handoff=TICKET_HANDOFF):
    bot.configure(lambda c: c.update(handoff=handoff))


def handback_bot(api, admin, **overrides):
    bot = make_store_bot(api, admin)
    bot.configure(lambda c: c["actions"].append({**REFUND, **overrides}))
    return bot


def staff_bot(api, admin):
    bot = make_store_bot(api, admin)

    def mutate(config):
        for action in config["actions"]:
            if action["name"] == "create_ticket":
                action["confirmation"] = "staff"

    bot.configure(mutate)
    return bot


def tool_result(api, bot, session, call_id, **body):
    return api.post(
        f"/v1/agents/{bot.id}/tool-results",
        headers=bot.auth,
        json={"session_id": session, "call_id": call_id, **body},
    )


def transcript(role):
    with get_session_factory()() as db:
        return list(db.scalars(select(Message).where(Message.role == role).order_by(Message.created_at)))


# ---------- handoff automation ----------


def test_a_handoff_can_open_a_ticket_in_the_companys_system(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    set_handoff(bot)
    fake_llm.script(HANDOFF)
    r = bot.send("I want to talk to someone", end_user=CUSTOMER).json()

    assert r["status"] == "handed_off"
    assert r["handoff"]["reason"] == "Customer is upset"
    assert r["handoff"]["action_result"]["ok"] is True
    assert r["handoff"]["action_result"]["data"]["status"] == "open"
    (post,) = company.posts()
    assert post["path"] == "/v1/tickets"
    assert post["body"] == {"subject": "Customer is upset", "customer_id": "cust_1"}
    assert ("action", "create_ticket", "ok") in steps(r["run_id"])


def test_a_failing_handoff_action_still_hands_off(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    set_handoff(bot)
    company.fail_status = 500
    fake_llm.script(HANDOFF)
    r = bot.send("I want to talk to someone", end_user=CUSTOMER).json()
    assert r["status"] == "handed_off"
    assert r["handoff"]["action_result"] == {"action": "create_ticket", "ok": False, "error": "http_error"}


def test_handoff_without_automation_just_reports_it(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    fake_llm.script(HANDOFF)
    r = bot.send("I want to talk to someone", end_user=CUSTOMER).json()
    assert r["handoff"] == {"reason": "Customer is upset", "summary": "Refund dispute on ACM-1002"}
    assert company.calls == []


def test_the_handoff_event_carries_what_happened(api, acme_admin, company, fake_llm, receiver) -> None:
    bot = make_store_bot(api, acme_admin)
    set_handoff(bot)
    make_hook(api, acme_admin, events=["handoff.requested"])
    fake_llm.script(HANDOFF)
    bot.send("I want to talk to someone", end_user=CUSTOMER, session_id="s9")
    (event,) = receiver.events("handoff.requested")
    assert event["data"]["session_id"] == "s9" and event["data"]["end_user_id"] == CUSTOMER["id"]
    assert event["data"]["handoff"]["summary"] == "Refund dispute on ACM-1002"
    assert event["data"]["handoff"]["action_result"]["ok"] is True


def test_a_handback_handoff_is_left_for_the_companys_backend(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    ticket = {
        "name": "notify_team",
        "description": "Tell the team.",
        "execution": "handback",
        "effect": "write",
        "parameters": {"note": {"type": "string", "required": True}},
    }
    bot.configure(lambda c: c["actions"].append(ticket))
    set_handoff(bot, {"action": "notify_team", "args": {"note": "{{handoff.summary}}"}})
    fake_llm.script(HANDOFF)
    r = bot.send("I want to talk to someone", end_user=CUSTOMER).json()
    assert r["status"] == "handed_off"
    assert r["handoff"]["action_result"] == {
        "action": "notify_team",
        "ok": None,
        "handback": True,
        "arguments": {"note": "Refund dispute on ACM-1002"},
    }
    assert company.calls == []


def test_workflow_handoffs_run_the_automation_too(api, acme_admin, company, fake_llm) -> None:
    bot = make_workflow_bot(
        api,
        acme_admin,
        [template_workflow("return_request")],
        handoff={
            "action": "create_ticket",
            "args": {"subject": "{{handoff.reason}}", "details": "{{handoff.summary}}"},
        },
    )
    fake_llm.script({"type": "start_workflow", "name": "return_request"})
    fake_llm.extract({"order_number": "ACM-1003"})  # delivered 45 days ago: out of the window
    r = bot.send("return ACM-1003", end_user=CUSTOMER, session_id="s1").json()
    assert r["status"] == "handed_off" and r["handoff"]["action_result"]["ok"] is True
    (post,) = company.posts()
    assert post["path"] == "/v1/tickets" and "45 days" in post["body"]["body"]


def test_the_handoff_configuration_is_checked_when_saved(api, acme_admin) -> None:
    bot = make_store_bot(api, acme_admin)
    config = api.get(f"/admin/agents/{bot.id}", headers=acme_admin).json()["draft_config"]
    bad = [
        {"action": "nope", "args": {}},  # unknown action
        {"action": "create_ticket", "args": {}},  # missing required subject
        {"action": "create_ticket", "args": {"subject": "x", "extra": "y"}},  # not a parameter
        {"action": "create_ticket", "args": {"subject": "{{vars.secret}}"}},  # not a handoff value
        {"action": "create_ticket", "args": {"subject": "{{handoff.password}}"}},
    ]
    for handoff in bad:
        saved = api.put(
            f"/admin/agents/{bot.id}/draft", headers=acme_admin, json={**config, "handoff": handoff}
        )
        assert saved.status_code == 422, handoff
    good = api.put(
        f"/admin/agents/{bot.id}/draft", headers=acme_admin, json={**config, "handoff": TICKET_HANDOFF}
    )
    assert good.status_code == 200


# ---------- hand-back: the company's backend performs the action ----------


def test_a_handback_action_goes_to_the_companys_backend_and_back(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin)
    fake_llm.script(REFUND_CALL)
    r = bot.send("Refund ACM-1002", end_user=CUSTOMER, session_id="s1").json()

    assert r["status"] == "action_required" and r["outcome"] == "action_required"
    request = r["action_request"]
    assert request["name"] == "issue_refund" and request["call_id"].startswith("call_")
    assert request["arguments"] == {"order_number": "ACM-1002", "amount_cents": 1999}
    assert company.calls == []  # we hold no credentials for this and call nothing

    state = api.get(f"/v1/agents/{bot.id}/sessions/s1", headers=bot.auth).json()
    assert state["active_run"]["status"] == "waiting_tool_result"
    assert state["active_run"]["action_request"]["call_id"] == request["call_id"]

    blocked = bot.send("hello?", end_user=CUSTOMER, session_id="s1")
    assert blocked.status_code == 409 and error(blocked) == "awaiting_tool_result"

    fake_llm.script(reply("Your refund of $19.99 was issued."))
    done = tool_result(
        api, bot, "s1", request["call_id"], result={"refund_id": "RF-9", "status": "issued", "internal": "x"}
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert (body["status"], body["run_id"]) == ("completed", r["run_id"])
    assert body["reply"] == "Your refund of $19.99 was issued."
    assert body["actions_taken"] == [{"name": "issue_refund", "effect": "write", "ok": True}]

    prompt = json.dumps(fake_llm.decide_prompts()[-1])
    assert "ACTION RESULT for issue_refund" in prompt and "RF-9" in prompt
    assert "internal" not in prompt  # only the exposed fields reach the model

    assert [m.content for m in transcript(MessageRole.user)] == ["Refund ACM-1002"]  # recorded once
    assert [m.content for m in transcript(MessageRole.assistant)] == ["Your refund of $19.99 was issued."]
    assert run_row(r["run_id"]).status == "completed"
    assert [t for t, *_ in steps(r["run_id"])].count("handback") == 1
    assert api.get(f"/v1/agents/{bot.id}/sessions/s1", headers=bot.auth).json()["active_run"] is None


def test_the_backend_can_report_that_the_action_failed(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin)
    fake_llm.script(REFUND_CALL)
    request = bot.send("Refund ACM-1002", end_user=CUSTOMER, session_id="s1").json()["action_request"]
    fake_llm.script(reply("Sorry, the refund could not be issued."))
    done = tool_result(api, bot, "s1", request["call_id"], error="card was already refunded").json()
    assert done["actions_taken"] == [
        {"name": "issue_refund", "effect": "write", "ok": False, "error": "handback_failed"}
    ]
    assert "card was already refunded" in json.dumps(fake_llm.decide_prompts()[-1])


def test_each_call_id_can_be_answered_once_and_only_by_its_own_agent(
    api, acme_admin, company, fake_llm
) -> None:
    bot = handback_bot(api, acme_admin)
    fake_llm.script(REFUND_CALL)
    request = bot.send("Refund ACM-1002", end_user=CUSTOMER, session_id="s1").json()["action_request"]

    wrong = tool_result(api, bot, "s1", "call_doesnotexist", result={})
    assert wrong.status_code == 404 and error(wrong) == "tool_call_not_found"
    assert tool_result(api, bot, "missing-session", request["call_id"], result={}).status_code == 404

    other = make_bot(api, acme_admin)  # a different agent's key
    assert tool_result(api, other, "s1", request["call_id"], result={}).status_code == 404

    fake_llm.script(reply("Done."))
    assert tool_result(api, bot, "s1", request["call_id"], result={"refund_id": "RF-1"}).status_code == 200
    again = tool_result(api, bot, "s1", request["call_id"], result={"refund_id": "RF-2"})
    assert again.status_code == 404  # a result cannot be applied twice


def test_oversized_results_are_refused(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin)
    fake_llm.script(REFUND_CALL)
    request = bot.send("Refund ACM-1002", end_user=CUSTOMER, session_id="s1").json()["action_request"]
    huge = tool_result(api, bot, "s1", request["call_id"], result={"blob": "x" * 70_000})
    assert huge.status_code == 422


def test_an_unanswered_call_expires_and_the_conversation_moves_on(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin)
    fake_llm.script(REFUND_CALL)
    r = bot.send("Refund ACM-1002", end_user=CUSTOMER, session_id="s1").json()
    with get_session_factory()() as db:
        run = db.get(Run, uuid.UUID(r["run_id"]))
        run.pending = {
            **run.pending,
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        }
        db.commit()

    late = tool_result(api, bot, "s1", r["action_request"]["call_id"], result={})
    assert late.status_code == 404
    fake_llm.script(reply("How can I help?"))
    fresh = bot.send("hello", end_user=CUSTOMER, session_id="s1")
    assert fresh.status_code == 200 and run_row(r["run_id"]).status == "expired"


def test_a_sweep_closes_abandoned_waiting_runs(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin)
    fake_llm.script(REFUND_CALL)
    r = bot.send("Refund ACM-1002", end_user=CUSTOMER, session_id="s1").json()
    with get_session_factory()() as db:
        db.get(Run, uuid.UUID(r["run_id"])).expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    with get_session_factory()() as db:
        assert housekeeping.run_sweep(db)["runs_expired"] == 1
    assert run_row(r["run_id"]).status == "expired"


def test_guards_stop_a_handback_action_before_it_leaves(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin)
    fake_llm.script(
        {**REFUND_CALL, "arguments": {"order_number": "ACM-1002", "amount_cents": 50_000}},
        reply("A person needs to handle that one."),
    )
    r = bot.send("Refund everything", end_user=CUSTOMER).json()
    assert r["status"] == "completed" and r["action_request"] is None
    assert "Refunds over $200 need a person." in json.dumps(fake_llm.decide_prompts()[-1])


def test_handback_can_still_ask_the_customer_first(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin, confirmation=None)  # a write action: the customer approves
    fake_llm.script(REFUND_CALL)
    asked = bot.send("Refund ACM-1002", end_user=CUSTOMER, session_id="s1").json()
    assert asked["status"] == "needs_confirmation"
    assert company.calls == []

    approved = api.post(
        f"/v1/agents/{bot.id}/confirmations",
        headers=bot.auth,
        json={
            "session_id": "s1",
            "confirmation_id": asked["pending_confirmation"]["id"],
            "decision": "confirm",
        },
    ).json()
    assert approved["status"] == "action_required"
    assert approved["action_request"]["arguments"] == {"order_number": "ACM-1002", "amount_cents": 1999}

    fake_llm.script(reply("Refund issued."))
    done = tool_result(api, bot, "s1", approved["action_request"]["call_id"], result={"refund_id": "RF-3"})
    assert done.json()["status"] == "completed"


def test_the_playground_answers_a_handback_action_from_its_mock(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin)
    fake_llm.script(REFUND_CALL, reply("Refunded (test)."))
    r = api.post(
        f"/admin/agents/{bot.id}/playground/messages", headers=acme_admin, json={"message": "refund it"}
    ).json()
    assert r["status"] == "completed" and r["action_request"] is None
    assert r["actions_taken"] == [{"name": "issue_refund", "effect": "write", "ok": True}]
    assert "RF-MOCK" in json.dumps(fake_llm.decide_prompts()[-1])


def test_workflows_can_hand_a_step_back_too(api, acme_admin, company, fake_llm) -> None:
    bot = make_workflow_bot(api, acme_admin, [template_workflow("return_request")])

    def to_handback(config):
        for action in config["actions"]:
            if action["name"] == "create_return":
                action["execution"] = "handback"
                action.pop("request")
                action.pop("connection_id")

    bot.configure(to_handback)
    fake_llm.script({"type": "start_workflow", "name": "return_request"})
    fake_llm.extract({"order_number": "ACM-1002"})
    bot.send("return ACM-1002", end_user=CUSTOMER, session_id="s1")
    fake_llm.extract({"reason": "stopped working"})
    bot.send("it stopped working", end_user=CUSTOMER, session_id="s1")
    waiting = bot.send("yes", end_user=CUSTOMER, session_id="s1").json()

    assert waiting["status"] == "action_required"
    assert waiting["action_request"]["name"] == "create_return"
    assert waiting["action_request"]["arguments"] == {"order_number": "ACM-1002", "reason": "stopped working"}
    assert company.posts() == []

    done = tool_result(
        api,
        bot,
        "s1",
        waiting["action_request"]["call_id"],
        result={"return_id": "R-77", "label_url": "https://l.test/77", "status": "created"},
    ).json()
    assert done["status"] == "completed" and "R-77" in done["reply"]
    assert run_row(done["run_id"]).status == "completed"


# ---------- staff approvals ----------


def test_a_staff_approved_action_waits_then_runs_exactly_as_shown(
    api, acme_admin, company, fake_llm, receiver
) -> None:
    bot = staff_bot(api, acme_admin)
    make_hook(api, acme_admin, events=["approval.requested", "approval.decided"])
    fake_llm.script(TICKET)
    r = bot.send("Open a ticket", end_user=CUSTOMER, session_id="s1").json()

    assert r["status"] == "pending_approval" and r["outcome"] == "pending_approval"
    assert r["pending_approval"]["action"] == "create_ticket"
    assert "Open a support ticket: Broken headphones" in r["pending_approval"]["summary"]
    assert "team" in r["reply"] and company.calls == []

    listing = api.get("/admin/approvals?status=pending", headers=acme_admin).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert (item["action"], item["end_user_ref"], item["agent_name"]) == (
        "create_ticket",
        "web-user-1",
        "Store bot",
    )
    assert item["args"] == {"subject": "Broken headphones"}

    (requested,) = receiver.events("approval.requested")
    assert requested["data"]["approval_id"] == item["id"] and requested["data"]["session_id"] == "s1"

    # The customer can keep talking while they wait; the request stays queued for staff.
    fake_llm.script(reply("Your request is with our team."))
    assert bot.send("any news?", end_user=CUSTOMER, session_id="s1").json()["status"] == "completed"
    assert api.get("/admin/approvals?status=pending", headers=acme_admin).json()["total"] == 1

    fake_llm.script(reply("Your ticket T-1 is open."))
    decided = api.post(
        f"/admin/approvals/{item['id']}/decision",
        headers=acme_admin,
        json={"decision": "approve", "note": "ok"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "completed" and decided.json()["reply"] == "Your ticket T-1 is open."

    (post,) = company.posts()
    assert post["body"] == {"subject": "Broken headphones", "customer_id": "cust_1"}
    assert post["headers"]["idempotency-key"].startswith(r["run_id"])

    (event,) = receiver.events("approval.decided")
    assert event["data"]["approved"] is True and event["data"]["decided_by"] == "acme.admin@example.com"
    assert event["data"]["reply"] == "Your ticket T-1 is open." and event["data"]["session_id"] == "s1"

    row = api.get("/admin/approvals?status=approved", headers=acme_admin).json()["items"][0]
    assert row["decided_by"] == "acme.admin@example.com" and row["note"] == "ok"
    assert run_row(r["run_id"]).status == "completed"

    again = api.post(
        f"/admin/approvals/{item['id']}/decision", headers=acme_admin, json={"decision": "approve"}
    )
    assert again.status_code == 409 and len(company.posts()) == 1  # never runs twice


def test_a_denied_action_never_runs(api, acme_admin, company, fake_llm, receiver) -> None:
    bot = staff_bot(api, acme_admin)
    make_hook(api, acme_admin, events=["approval.decided"])
    fake_llm.script(TICKET)
    r = bot.send("Open a ticket", end_user=CUSTOMER, session_id="s1").json()
    approval_id = api.get("/admin/approvals", headers=acme_admin).json()["items"][0]["id"]

    decided = api.post(
        f"/admin/approvals/{approval_id}/decision",
        headers=acme_admin,
        json={"decision": "deny", "note": "Please contact billing."},
    ).json()
    assert decided["outcome"] == "denied" and "declined" in decided["reply"]
    assert "Please contact billing." in decided["reply"]
    assert company.calls == []
    assert run_row(r["run_id"]).status == "completed" and run_row(r["run_id"]).outcome == "denied"
    (event,) = receiver.events("approval.decided")
    assert event["data"]["approved"] is False and event["data"]["note"] == "Please contact billing."
    assert api.get("/admin/approvals?status=denied", headers=acme_admin).json()["total"] == 1


def test_approvals_are_private_to_their_workspace(api, acme_admin, globex_admin, company, fake_llm) -> None:
    bot = staff_bot(api, acme_admin)
    fake_llm.script(TICKET)
    bot.send("Open a ticket", end_user=CUSTOMER)
    approval_id = api.get("/admin/approvals", headers=acme_admin).json()["items"][0]["id"]
    assert api.get("/admin/approvals", headers=globex_admin).json() == {"items": [], "total": 0}
    foreign = api.post(
        f"/admin/approvals/{approval_id}/decision", headers=globex_admin, json={"decision": "approve"}
    )
    assert foreign.status_code == 404 and company.calls == []


def test_a_late_decision_is_refused_and_a_sweep_closes_the_request(
    api, acme_admin, company, fake_llm
) -> None:
    bot = staff_bot(api, acme_admin)
    fake_llm.script(TICKET)
    r = bot.send("Open a ticket", end_user=CUSTOMER).json()
    approval_id = api.get("/admin/approvals", headers=acme_admin).json()["items"][0]["id"]
    with get_session_factory()() as db:
        db.get(Approval, uuid.UUID(approval_id)).expires_at = datetime.now(timezone.utc) - timedelta(
            minutes=1
        )
        db.commit()

    late = api.post(
        f"/admin/approvals/{approval_id}/decision", headers=acme_admin, json={"decision": "approve"}
    )
    assert late.status_code == 409 and "expired" in late.json()["detail"]
    assert company.calls == []

    with get_session_factory()() as db:
        assert housekeeping.run_sweep(db)["approvals_expired"] == 0  # the failed decision already closed it
        assert db.get(Approval, uuid.UUID(approval_id)).status == "expired"
        db.get(Run, uuid.UUID(r["run_id"])).status = "waiting_approval"  # as if only the sweep had run
        db.get(Approval, uuid.UUID(approval_id)).status = "pending"
        db.commit()
        assert housekeeping.run_sweep(db)["approvals_expired"] == 1
    assert run_row(r["run_id"]).status == "expired"


def test_a_staff_approval_inside_a_workflow(api, acme_admin, company, fake_llm) -> None:
    bot = make_workflow_bot(api, acme_admin, [template_workflow("ticket_intake")])

    def staff_only(config):
        for action in config["actions"]:
            if action["name"] == "create_ticket":
                action["confirmation"] = "staff"

    bot.configure(staff_only)
    fake_llm.script({"type": "start_workflow", "name": "ticket_intake"})
    fake_llm.extract({"subject": "Broken headphones", "details": "The left ear stopped working."})
    asked = bot.send("I need to report a problem", end_user=CUSTOMER, session_id="s1").json()
    assert asked["status"] == "needs_confirmation"  # the template still asks the customer first
    waiting = bot.send("yes", end_user=CUSTOMER, session_id="s1").json()
    assert waiting["status"] == "pending_approval" and company.posts() == []

    approval_id = api.get("/admin/approvals?status=pending", headers=acme_admin).json()["items"][0]["id"]
    decided = api.post(
        f"/admin/approvals/{approval_id}/decision", headers=acme_admin, json={"decision": "approve"}
    ).json()
    assert decided["status"] == "completed" and "Your ticket number is T-" in decided["reply"]
    (post,) = company.posts()
    assert post["body"]["subject"] == "Broken headphones"
    assert run_row(waiting["run_id"]).status == "completed"


def test_the_playground_asks_the_tester_instead_of_queueing_staff(api, acme_admin, company, fake_llm) -> None:
    bot = staff_bot(api, acme_admin)
    fake_llm.script(TICKET)
    r = api.post(
        f"/admin/agents/{bot.id}/playground/messages", headers=acme_admin, json={"message": "open a ticket"}
    ).json()
    assert r["status"] == "needs_confirmation"
    assert api.get("/admin/approvals", headers=acme_admin).json()["total"] == 0
    with get_session_factory()() as db:
        assert db.scalars(select(Approval)).first() is None


def test_a_handback_action_is_tested_with_its_sample_only(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin)
    url = f"/admin/agents/{bot.id}/actions/issue_refund/test"
    body = {
        "arguments": {"order_number": "ACM-1002", "amount_cents": 100},
        "context": {},
        "mock": False,
        "send_for_real": True,
    }
    assert api.post(url, headers=acme_admin, json=body).status_code == 422
    sample = api.post(url, headers=acme_admin, json={**body, "mock": True}).json()
    assert sample["result"]["mock"] is True and sample["model_view"]["data"]["refund_id"] == "RF-MOCK"


def test_hosted_actions_still_need_a_connection_and_a_request(api, acme_admin, company) -> None:
    bot = make_store_bot(api, acme_admin)
    config = api.get(f"/admin/agents/{bot.id}", headers=acme_admin).json()["draft_config"]
    no_connection = {**config["actions"][0], "connection_id": None}
    no_request = {**config["actions"][0], "request": None}
    for broken in (no_connection, no_request):
        saved = api.put(
            f"/admin/agents/{bot.id}/draft", headers=acme_admin, json={**config, "actions": [broken]}
        )
        assert saved.status_code == 422, broken
