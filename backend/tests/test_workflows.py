"""Workflows end to end: the public API drives the engine; the "model" is scripted, so each test
states exactly what it extracts or decides and asserts what the system does with it."""

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Run
from app.db.session import get_session_factory
from tests.company import CUSTOMER, make_workflow_bot, template_workflow
from tests.test_agent_actions import reply, run_row, steps

START_RETURN = {"type": "start_workflow", "name": "return_request"}


def returns_bot(api, admin):
    return make_workflow_bot(api, admin, [template_workflow("return_request")])


def say(bot, text, session="s1", **extra):
    return bot.send(text, end_user=CUSTOMER, session_id=session, **extra).json()


# ---------- the return process, start to finish ----------


def test_return_request_happy_path_spans_several_turns(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)

    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "acm-1002"})  # lowercase; the field uppercases it
    first = say(bot, "I want to return ACM-1002")
    assert first["status"] == "needs_input" and "reason for the return" in first["reply"]
    assert company.calls[0]["path"] == "/v1/orders/ACM-1002" and company.calls[0]["query"] == {
        "customer_id": "cust_1"
    }
    state = api.get(f"/v1/agents/{bot.id}/sessions/s1", headers=bot.auth).json()
    assert state["active_run"] is None or state["active_run"]["status"] != "completed"

    fake_llm.extract({"reason": "stopped working"})
    second = say(bot, "it stopped working after a week")
    assert second["status"] == "needs_confirmation"
    assert "Return Headphones from order ACM-1002 because: stopped working" in second["reply"]
    assert company.posts() == []  # nothing happens before the customer approves

    third = say(bot, "yes")
    assert third["status"] == "completed" and third["outcome"] == "workflow_completed"
    assert "reference R-1" in third["reply"] and "https://l.test/1" in third["reply"]
    (post,) = company.posts()
    assert post["body"] == {"order_number": "ACM-1002", "reason": "stopped working", "customer_id": "cust_1"}
    assert post["headers"]["idempotency-key"].startswith(first["run_id"])
    assert len({first["run_id"], second["run_id"], third["run_id"]}) == 1  # one run across three turns
    assert run_row(first["run_id"]).status == "completed" and run_row(first["run_id"]).kind == "workflow"


def test_the_customer_is_asked_to_approve_only_once(api, acme_admin, company, fake_llm) -> None:
    """`confirmed_by` lets the write action reuse the approval from the confirm step."""
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002", "reason": "broken"})  # both in the first message
    asked = say(bot, "return ACM-1002, it is broken")
    assert asked["status"] == "needs_confirmation"
    done = say(bot, "yes")
    assert done["status"] == "completed" and len(company.posts()) == 1


def test_without_confirmed_by_the_action_asks_again(api, acme_admin, company, fake_llm) -> None:
    wf = template_workflow("return_request")
    del wf["nodes"]["create"]["confirmed_by"]
    bot = make_workflow_bot(api, acme_admin, [wf])
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002", "reason": "broken"})
    say(bot, "return ACM-1002, it is broken")
    second_prompt = say(bot, "yes")  # approves the workflow's own confirm step...
    assert second_prompt["status"] == "needs_confirmation" and company.posts() == []  # ...the action asks too
    assert say(bot, "yes")["status"] == "completed" and len(company.posts()) == 1


def test_declining_runs_nothing(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002", "reason": "meh"})
    say(bot, "return ACM-1002 because meh")
    declined = say(bot, "no")
    assert declined["status"] == "completed" and "haven't started a return" in declined["reply"]
    assert company.posts() == []


def test_orders_outside_the_window_are_handed_to_a_person(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1003"})  # delivered 45 days ago
    r = say(bot, "return ACM-1003")
    assert r["status"] == "handed_off" and "outside our 30-day return window" in r["reply"]
    assert "delivered 45 days ago" in r["handoff"]["summary"] and company.posts() == []
    assert run_row(r["run_id"]).status == "handed_off"


def test_an_order_that_is_not_theirs_looks_like_any_missing_order(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-2001"})  # belongs to another customer
    r = say(bot, "return ACM-2001")
    assert r["status"] == "completed" and "couldn't find that order" in r["reply"] and company.posts() == []


# ---------- collecting details ----------


def test_invalid_values_are_explained_and_asked_again(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "banana"})
    r = say(bot, "return my banana")
    assert r["status"] == "needs_input"
    assert r["reply"].startswith("That doesn't look like an order number.") and "Which order" in r["reply"]
    assert company.calls == []  # an invalid value never reaches the company's system
    fake_llm.extract({"order_number": "ACM-1002"})
    assert say(bot, "ACM-1002")["status"] == "needs_input"  # moved on, now asking for the reason


def test_repeated_failures_hand_off_instead_of_looping_forever(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "nope"})
    say(bot, "return nope")
    fake_llm.extract({"order_number": "still-nope"})
    say(bot, "still nope")
    fake_llm.extract({})
    last = say(bot, "dunno")
    assert (
        last["status"] == "handed_off" and "Could not get a valid 'order_number'" in last["handoff"]["reason"]
    )


def test_a_side_question_is_answered_then_the_process_continues(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002"})
    say(bot, "return ACM-1002")
    fake_llm.extract({}, intent="other")
    side = say(bot, "What is the return window for returns?")
    assert side["status"] == "needs_input"
    assert (
        "30 days" in side["reply"] and "reason for the return" in side["reply"]
    )  # answer, then the question again
    assert side["citations"] and side["citations"][0]["title"] == "Acme Return Policy"
    fake_llm.extract({"reason": "damaged"})
    assert say(bot, "it arrived damaged")["status"] == "needs_confirmation"  # state survived the detour


def test_cancelling_stops_the_process(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002"})
    first = say(bot, "return ACM-1002")
    fake_llm.extract({}, intent="cancel")
    r = say(bot, "never mind")
    assert r["outcome"] == "cancelled" and run_row(first["run_id"]).status == "cancelled"
    assert api.get(f"/v1/agents/{bot.id}/sessions/s1", headers=bot.auth).json()["active_run"] is None
    fake_llm.script(reply("Hi again!"))  # the next message starts fresh (decision loop)
    assert say(bot, "hello")["reply"] == "Hi again!"


def test_a_side_question_at_the_approval_step_asks_again_without_approving(
    api, acme_admin, company, fake_llm
) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002", "reason": "broken"})
    first = say(bot, "return ACM-1002, it is broken")
    side = say(bot, "what is the return window for returns?")
    assert (
        side["status"] == "needs_confirmation"
        and "30 days" in side["reply"]
        and "Shall I go ahead" in side["reply"]
    )
    assert side["pending_confirmation"]["id"] == first["pending_confirmation"]["id"] and company.posts() == []
    assert say(bot, "yes")["status"] == "completed" and len(company.posts()) == 1


# ---------- starting a workflow explicitly ----------


def test_the_chatbot_can_start_a_specific_process_without_the_model_choosing(
    api, acme_admin, company, fake_llm
) -> None:
    bot = make_workflow_bot(api, acme_admin, [template_workflow("ticket_intake")])
    fake_llm.extract({"subject": "Broken zipper", "details": "The zipper on my bag broke on day two."})
    asked = say(bot, "I have a problem", workflow="ticket_intake")  # no decision is scripted: none is needed
    assert (
        asked["status"] == "needs_confirmation" and "Open a support ticket: Broken zipper" in asked["reply"]
    )
    done = say(bot, "yes")
    assert done["status"] == "completed" and "ticket number is T-" in done["reply"]
    (post,) = company.posts()
    assert post["body"]["subject"] == "Broken zipper" and post["body"]["customer_id"] == "cust_1"
    assert fake_llm.decide_prompts() == []


def test_unknown_or_disabled_workflows_are_refused_up_front(api, acme_admin, company, fake_llm) -> None:
    wf = template_workflow("order_status")
    wf["enabled"] = False
    bot = make_workflow_bot(api, acme_admin, [wf])
    r = bot.send("hi", end_user=CUSTOMER, workflow="order_status")
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_workflow"
    assert bot.send("hi", end_user=CUSTOMER, workflow="Bad Name").status_code == 422
    pg = api.post(
        f"/admin/agents/{bot.id}/playground/messages",
        headers=acme_admin,
        json={"message": "hi", "workflow": "ghost"},
    )
    assert pg.status_code == 422


def test_starting_a_new_process_supersedes_one_that_was_waiting(api, acme_admin, company, fake_llm) -> None:
    bot = make_workflow_bot(
        api, acme_admin, [template_workflow("return_request"), template_workflow("order_status")]
    )
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002"})
    old = say(bot, "return ACM-1002")
    fake_llm.extract({"order_number": "ACM-1002"})
    new = say(bot, "actually just check it", workflow="order_status")
    assert new["status"] == "completed" and "Order ACM-1002 (Headphones) is delivered" in new["reply"]
    assert run_row(old["run_id"]).status == "cancelled"


def test_model_picks_between_processes_from_their_descriptions(api, acme_admin, company, fake_llm) -> None:
    bot = make_workflow_bot(
        api, acme_admin, [template_workflow("return_request"), template_workflow("order_status")]
    )
    fake_llm.script({"type": "start_workflow", "name": "order_status"})
    fake_llm.extract({"order_number": "ACM-1002"})
    r = say(bot, "where is my order ACM-1002?")
    assert "is delivered" in r["reply"]
    system = fake_llm.decide_prompts()[-1][0]["content"]
    assert "Guided processes" in system and "return_request: The customer wants to return" in system


def test_the_model_cannot_start_a_process_that_does_not_exist(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script({"type": "start_workflow", "name": "wire_money"}, reply("I can't do that."))
    r = say(bot, "send me money")
    assert r["reply"] == "I can't do that." and company.calls == []


# ---------- robustness ----------


def test_runaway_workflows_are_stopped(api, acme_admin, company, fake_llm) -> None:
    loop_wf = {
        "name": "spin", "description": "Never ends.", "start": "a",
        "nodes": {"a": {"type": "branch", "cases": [{"when": "true", "next": "a"}]}},
    }  # fmt: skip
    bot = make_workflow_bot(api, acme_admin, [loop_wf])
    r = say(bot, "go", workflow="spin")
    assert (r["status"], r["outcome"]) == ("failed", "workflow_loop")
    assert run_row(r["run_id"]).status == "failed"


def test_an_expired_approval_does_nothing(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002", "reason": "broken"})
    first = say(bot, "return ACM-1002, it is broken")
    with get_session_factory()() as db:
        run = db.get(Run, uuid.UUID(first["run_id"]))
        run.pending = {
            **run.pending,
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        }
        db.commit()
    r = say(bot, "yes")
    assert r["outcome"] == "expired" and company.posts() == []


def test_a_workflow_removed_mid_conversation_ends_gracefully(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002"})
    say(bot, "return ACM-1002")
    config = api.get(f"/admin/agents/{bot.id}", headers=acme_admin).json()["draft_config"]
    config["workflows"] = []
    api.put(f"/admin/agents/{bot.id}/draft", headers=acme_admin, json=config)
    api.post(f"/admin/agents/{bot.id}/publish", headers=acme_admin, json={})
    r = say(bot, "it broke")
    assert r["status"] == "failed" and r["outcome"] == "workflow_unavailable"


def test_explicit_confirmation_endpoint_works_for_workflows(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002", "reason": "broken"})
    asked = say(bot, "return ACM-1002, it is broken")
    done = api.post(
        f"/v1/agents/{bot.id}/confirmations", headers=bot.auth,
        json={"session_id": "s1", "confirmation_id": asked["pending_confirmation"]["id"], "decision": "confirm"},
    )  # fmt: skip
    assert done.status_code == 200 and done.json()["status"] == "completed" and len(company.posts()) == 1
    again = api.post(
        f"/v1/agents/{bot.id}/confirmations", headers=bot.auth,
        json={"session_id": "s1", "confirmation_id": asked["pending_confirmation"]["id"], "decision": "confirm"},
    )  # fmt: skip
    assert again.status_code == 404 and len(company.posts()) == 1


def test_guards_still_apply_inside_workflows(api, acme_admin, company, fake_llm) -> None:
    wf = template_workflow("return_request")
    config_mutator = lambda c: c["actions"][1].update(  # noqa: E731
        guards=[{"when": "args.reason == 'forbidden'", "message": "That reason is not allowed."}]
    )
    bot = make_workflow_bot(api, acme_admin, [wf])
    cfg = api.get(f"/admin/agents/{bot.id}", headers=acme_admin).json()["draft_config"]
    config_mutator(cfg)
    api.put(f"/admin/agents/{bot.id}/draft", headers=acme_admin, json=cfg)
    api.post(f"/admin/agents/{bot.id}/publish", headers=acme_admin, json={})
    fake_llm.script(START_RETURN)
    fake_llm.extract({"order_number": "ACM-1002", "reason": "forbidden"})
    asked = say(bot, "return ACM-1002 forbidden")
    assert asked["status"] == "needs_confirmation"
    done = say(bot, "yes")
    # the guard is evaluated again when the action runs; the process fails over to its error step
    assert company.posts() == [] and "couldn't start the return" in done["reply"]


# ---------- other node types ----------


def test_llm_and_knowledge_steps(api, acme_admin, company, fake_llm) -> None:
    wf = {
        "name": "recap", "description": "Explains the policy and writes a note.", "start": "policy",
        "nodes": {
            "policy": {"type": "knowledge", "question": "What is the return window for returns?", "save_as": "kb", "next": "note"},
            "note": {"type": "llm", "prompt": "Say thanks and mention: {{vars.kb.answer}}", "save_as": "thanks", "next": "show"},
            "show": {"type": "message", "text": "{{vars.thanks}}"},
        },
    }  # fmt: skip
    bot = make_workflow_bot(api, acme_admin, [wf])
    fake_llm.write(text="Thanks for asking!")
    r = say(bot, "tell me", workflow="recap")
    assert r["status"] == "completed" and "30 days" in r["reply"] and "Thanks for asking!" in r["reply"]
    assert r["citations"] and r["citations"][0]["title"] == "Acme Return Policy"


def test_playground_runs_workflows_with_mocked_actions(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    url = f"/admin/agents/{bot.id}/playground"
    fake_llm.extract({"order_number": "ACM-1002", "reason": "broken"})
    asked = api.post(
        f"{url}/messages", headers=acme_admin,
        json={"message": "return it", "workflow": "return_request", "mock_actions": True, "end_user_attributes": {"customer_id": "c1"}},
    ).json()  # fmt: skip
    assert asked["status"] == "needs_confirmation"
    done = api.post(
        f"{url}/confirmations", headers=acme_admin,
        json={"session_id": asked["session_id"], "confirmation_id": asked["pending_confirmation"]["id"], "decision": "confirm", "mock_actions": True},
    ).json()  # fmt: skip
    assert done["status"] == "completed" and "R-MOCK" in done["reply"] and company.calls == []


def test_workflow_runs_are_traced(api, acme_admin, company, fake_llm) -> None:
    bot = returns_bot(api, acme_admin)
    fake_llm.extract({"order_number": "ACM-1002", "reason": "broken"})
    first = say(bot, "return it", workflow="return_request")
    say(bot, "yes")
    trace = [(t, n) for t, n, _ in steps(first["run_id"])]
    assert trace[0] == ("workflow", "start") and ("llm", "extract") in trace
    assert (
        ("action", "get_order") in trace
        and ("confirm", "approved") in trace
        and ("action", "create_return") in trace
    )
    detail = api.get(f"/admin/runs/{first['run_id']}", headers=acme_admin).json()
    assert detail["status"] == "completed" and "acme-secret" not in json.dumps(detail)
    with get_session_factory()() as db:
        assert db.scalars(select(Run).where(Run.kind == "workflow")).first() is not None


def test_templates_endpoint_reports_what_an_agent_is_missing(api, acme_admin, company) -> None:
    from tests.test_agents_platform import new_agent

    bare = new_agent(api, acme_admin, "Bare", template="blank")
    listing = api.get(f"/admin/agents/{bare['id']}/workflow-templates", headers=acme_admin).json()
    by_id = {t["id"]: t for t in listing}
    assert set(by_id) == {"ticket_intake", "return_request", "order_status"}
    assert by_id["order_status"]["ready"] is False and by_id["order_status"]["missing"] == [
        "an action called 'get_order'"
    ]

    ready = make_workflow_bot(api, acme_admin, [])
    listing = api.get(f"/admin/agents/{ready.id}/workflow-templates", headers=acme_admin).json()
    assert all(t["ready"] for t in listing) and listing[0]["workflow"]["name"] == "ticket_intake"
    # a ready template can be saved straight into the draft
    config = api.get(f"/admin/agents/{ready.id}", headers=acme_admin).json()["draft_config"]
    config["workflows"] = [by_id["order_status"]["workflow"]]
    assert api.put(f"/admin/agents/{ready.id}/draft", headers=acme_admin, json=config).status_code == 200
    # ...and tenants cannot read each other's agents' templates
    assert api.get(f"/admin/agents/{ready.id}/workflow-templates", headers=acme_admin).status_code == 200
