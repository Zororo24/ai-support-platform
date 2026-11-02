"""Phase L: scenario tests, the publish gate, rollback, version diff, recording and replay.

The "model" is scripted, so a scenario's pass or fail depends only on what the platform does with
the decisions it is given: which actions run, which are refused, what state the conversation ends in.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import AuditLog, Conversation
from app.db.session import get_session_factory
from app.services import housekeeping
from tests.company import CUSTOMER, make_store_bot, make_workflow_bot, template_workflow
from tests.test_agent_actions import GET_1002, TICKET, reply
from tests.test_agents_platform import edit_and_publish
from tests.test_jobs_and_webhooks import make_hook
from tests.test_public_api import make_bot

ATTRS = {"customer_id": "cust_1"}


def definition(steps, *, mocks=None, workflow=None, attrs=None):
    body = {"end_user": {"id": "tester", "attributes": attrs or ATTRS}, "steps": steps}
    if mocks:
        body["mocks"] = mocks
    if workflow:
        body["workflow"] = workflow
    return body


def lookup_step(**expect):
    return {"say": "Where is ACM-1002?", "expect": expect}


def add(api, admin, bot, name, body, **extra):
    return api.post(
        f"/admin/agents/{bot.id}/scenarios", headers=admin, json={"name": name, "definition": body, **extra}
    )


def run_tests(api, admin, bot, target="draft", **extra):
    return api.post(f"/admin/agents/{bot.id}/scenarios/run", headers=admin, json={"target": target, **extra})


def publish(api, admin, bot, **extra):
    return api.post(f"/admin/agents/{bot.id}/publish", headers=admin, json={"note": "x", **extra})


def break_get_order(config):
    """Rename the order parameter, so the model's calls no longer match the action's schema."""
    action = next(a for a in config["actions"] if a["name"] == "get_order")
    action["parameters"] = {"order_no": action["parameters"]["order_number"]}
    action["request"]["path"] = "/orders/{{args.order_no}}"


def save_draft(api, admin, bot, mutate):
    config = api.get(f"/admin/agents/{bot.id}", headers=admin).json()["draft_config"]
    mutate(config)
    saved = api.put(f"/admin/agents/{bot.id}/draft", headers=admin, json=config)
    assert saved.status_code == 200, saved.text


GOOD_LOOKUP = definition(
    [
        lookup_step(
            status="completed",
            actions_called=[{"name": "get_order", "arguments": {"order_number": "ACM-1002"}}],
            actions_not_called=["create_ticket"],
            reply_contains=["delivered"],
        )
    ]
)


# ---------- storing scenarios ----------


def test_scenarios_are_validated_and_listed(api, acme_admin, company) -> None:
    bot = make_store_bot(api, acme_admin)
    created = add(api, acme_admin, bot, "Order lookup", GOOD_LOOKUP)
    assert created.status_code == 201, created.text
    assert created.json()["definition"]["steps"][0]["say"] == "Where is ACM-1002?"

    assert add(api, acme_admin, bot, "Order lookup", GOOD_LOOKUP).status_code == 409  # names are unique
    bad = [
        definition([{"expect": {}}]),  # a step must say or confirm something
        definition([{"say": "hi", "confirm": "yes"}]),  # not both
        definition([]),  # at least one step
        definition([{"say": "hi", "expect": {"status": "exploded"}}]),
        definition([{"say": "hi", "expect": {"nonsense": 1}}]),  # unknown keys are errors, not ignored
        definition([{"say": "hi"}], mocks={"get_order": {"result": 1, "sequence": [{"result": 2}]}}),
        definition([{"say": "hi"}] * 13),
    ]
    for body in bad:
        assert add(api, acme_admin, bot, "Bad", body).status_code == 422, body

    listed = api.get(f"/admin/agents/{bot.id}/scenarios", headers=acme_admin).json()
    assert [s["name"] for s in listed] == ["Order lookup"] and listed[0]["last_result"] is None

    sid = created.json()["id"]
    renamed = api.put(
        f"/admin/agents/{bot.id}/scenarios/{sid}",
        headers=acme_admin,
        json={"name": "Lookup", "enabled": False},
    )
    assert renamed.status_code == 200 and renamed.json()["enabled"] is False
    assert api.delete(f"/admin/agents/{bot.id}/scenarios/{sid}", headers=acme_admin).status_code == 204
    assert api.get(f"/admin/agents/{bot.id}/scenarios", headers=acme_admin).json() == []


def test_scenarios_are_private_to_their_workspace(api, acme_admin, globex_admin, company) -> None:
    bot = make_store_bot(api, acme_admin)
    sid = add(api, acme_admin, bot, "Order lookup", GOOD_LOOKUP).json()["id"]
    base = f"/admin/agents/{bot.id}"
    assert api.get(f"{base}/scenarios", headers=globex_admin).status_code == 404
    assert (
        api.put(f"{base}/scenarios/{sid}", headers=globex_admin, json={"enabled": False}).status_code == 404
    )
    assert api.delete(f"{base}/scenarios/{sid}", headers=globex_admin).status_code == 404
    assert run_tests(api, globex_admin, bot).status_code == 404
    assert api.get(f"{base}/diff", headers=globex_admin).status_code == 404


# ---------- running them ----------


def test_a_passing_scenario_checks_behaviour_without_touching_the_real_system(
    api, acme_admin, company, fake_llm
) -> None:
    bot = make_store_bot(api, acme_admin)
    add(api, acme_admin, bot, "Order lookup", GOOD_LOOKUP)
    fake_llm.script(GET_1002, reply("Your headphones were delivered 12 days ago."))
    run = run_tests(api, acme_admin, bot)
    assert run.status_code == 200, run.text
    body = run.json()
    assert (body["passed"], body["failed"], body["target"], body["purpose"]) == (1, 0, "draft", "manual")
    (result,) = body["results"]
    assert result["passed"] is True and result["error"] is None
    (step,) = result["steps"]
    assert step["failures"] == [] and step["status"] == "completed"
    assert step["actions_called"] == [
        {"name": "get_order", "arguments": {"order_number": "ACM-1002"}, "ok": True}
    ]
    assert company.calls == []  # answered from the sample response, never the company's API

    listed = api.get(f"/admin/agents/{bot.id}/scenarios", headers=acme_admin).json()
    assert listed[0]["last_result"]["passed"] is True and listed[0]["last_result"]["target"] == "draft"
    history = api.get(f"/admin/agents/{bot.id}/scenario-runs", headers=acme_admin).json()
    assert len(history) == 1 and history[0]["passed"] == 1
    detail = api.get(f"/admin/agents/{bot.id}/scenario-runs/{history[0]['id']}", headers=acme_admin).json()
    assert detail["results"][0]["name"] == "Order lookup"


def test_failures_say_exactly_what_was_expected(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    expectations = dict(
        status="needs_input",
        outcome="answered",
        reply_contains=["refund"],
        reply_not_contains=["delivered"],
        cites=True,
        actions_called=[
            {"name": "get_order", "arguments": {"order_number": "ACM-9999"}},
            {"name": "create_ticket"},
        ],
        actions_not_called=["get_order"],
        workflow_started="return_request",
    )
    add(api, acme_admin, bot, "Everything wrong", definition([lookup_step(**expectations)]))
    fake_llm.script(GET_1002, reply("Your headphones were delivered 12 days ago."))
    (result,) = run_tests(api, acme_admin, bot).json()["results"]
    failures = " | ".join(result["steps"][0]["failures"])
    assert result["passed"] is False
    for expected in (
        "status to be “needs_input” but it was “completed”",
        "outcome to be “answered” but it was “replied”",
        "reply to mention “refund”",
        "should not mention “delivered”",
        "cite a document",
        "called with {'order_number': 'ACM-1002'}",
        "Expected create_ticket to be called, but it was not",
        "get_order should not have been called",
        "process “return_request” to start, but it was none",
    ):
        assert expected in failures, (expected, failures)


def test_buttons_and_scripted_action_results(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    body = definition(
        [
            {"say": "Open a ticket for my headphones", "expect": {"status": "needs_confirmation", "actions_not_called": ["create_ticket"]}},
            {"confirm": "yes", "expect": {"status": "completed", "actions_called": [{"name": "create_ticket"}], "reply_contains": ["T-9"]}},
        ],
        mocks={"create_ticket": {"result": {"ticket_id": "T-9", "status": "open"}}},
    )  # fmt: skip
    add(api, acme_admin, bot, "Ticket", body)
    fake_llm.script(TICKET, reply("Done: your ticket is T-9."))
    (result,) = run_tests(api, acme_admin, bot).json()["results"]
    assert result["passed"] is True, result
    assert company.calls == []
    assert "T-9" in json.dumps(fake_llm.decide_prompts()[-1])  # the scripted result reached the model


def test_a_scripted_failure_is_what_the_model_sees(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    body = definition(
        [lookup_step(status="completed", reply_contains=["try again"])],
        mocks={"get_order": {"error": "the orders system timed out"}},
    )
    add(api, acme_admin, bot, "Orders system down", body)
    fake_llm.script(GET_1002, reply("Sorry, please try again later."))
    (result,) = run_tests(api, acme_admin, bot).json()["results"]
    assert result["passed"] is True
    assert result["steps"][0]["actions_called"][0]["ok"] is False
    assert "timed out" in json.dumps(fake_llm.decide_prompts()[-1])


def test_a_sequence_of_results_for_repeated_calls(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    body = definition(
        [lookup_step(status="completed")],
        mocks={"get_order": {"sequence": [{"error": "temporary"}, {"result": {"status": "delivered"}}]}},
    )
    add(api, acme_admin, bot, "Retry", body)
    fake_llm.script(GET_1002, GET_1002, reply("It was delivered."))
    (result,) = run_tests(api, acme_admin, bot).json()["results"]
    assert [c["ok"] for c in result["steps"][0]["actions_called"]] == [False, True]


def test_workflow_scenarios_follow_the_process(api, acme_admin, company, fake_llm) -> None:
    bot = make_workflow_bot(api, acme_admin, [template_workflow("return_request")])
    old_order = {
        "get_order": {
            "result": {
                "order_number": "ACM-1003",
                "status": "delivered",
                "item_name": "Case",
                "days_since_delivery": 45,
            }
        }
    }  # noqa: E501
    add(api, acme_admin, bot, "Too late", definition(
        [{"say": "return ACM-1003", "expect": {"status": "handed_off", "workflow_started": "return_request", "actions_not_called": ["create_return"]}}],
        mocks=old_order, workflow="return_request",
    ))  # fmt: skip
    add(api, acme_admin, bot, "In time", definition(
        [{"say": "return ACM-1002", "expect": {"status": "needs_input", "actions_called": [{"name": "get_order"}]}}],
        workflow="return_request",
    ))  # fmt: skip
    fake_llm.extract({"order_number": "ACM-1003"})
    fake_llm.extract({"order_number": "ACM-1002"})
    body = run_tests(api, acme_admin, bot).json()
    assert (body["passed"], body["failed"]) == (2, 0), body["results"]
    assert company.calls == []


def test_a_conversation_that_cannot_run_fails_the_scenario_clearly(
    api, acme_admin, company, fake_llm
) -> None:
    bot = make_store_bot(api, acme_admin)
    add(api, acme_admin, bot, "Nothing to confirm", definition([{"confirm": "yes"}]))
    add(api, acme_admin, bot, "Unknown process", definition([{"say": "hi"}], workflow="not_a_process"))
    results = {r["name"]: r for r in run_tests(api, acme_admin, bot).json()["results"]}
    assert "nothing waiting to be confirmed" in results["Nothing to confirm"]["error"]
    assert "no process called" in results["Unknown process"]["error"]
    assert not any(r["passed"] for r in results.values())


def test_disabled_scenarios_are_skipped_and_versions_can_be_chosen(
    api, acme_admin, company, fake_llm
) -> None:
    bot = make_store_bot(api, acme_admin)
    add(api, acme_admin, bot, "Off", GOOD_LOOKUP, enabled=False)
    assert run_tests(api, acme_admin, bot).json()["results"] == []
    add(api, acme_admin, bot, "On", GOOD_LOOKUP)
    fake_llm.script(GET_1002, reply("It was delivered."))
    assert run_tests(api, acme_admin, bot, target="1").json()["target"] == "v1"
    assert run_tests(api, acme_admin, bot, target="99").status_code == 404
    assert run_tests(api, acme_admin, bot, target="banana").status_code == 422


def test_tests_never_notify_production_systems(api, acme_admin, company, fake_llm, receiver) -> None:
    bot = make_store_bot(api, acme_admin)
    make_hook(api, acme_admin)
    add(api, acme_admin, bot, "Order lookup", GOOD_LOOKUP)
    fake_llm.script(GET_1002, reply("It was delivered."))
    run_tests(api, acme_admin, bot)
    assert receiver.requests == []
    runs = api.get("/admin/runs?source=test", headers=acme_admin).json()
    assert runs["total"] >= 1  # the traces are there to open and read


def test_old_test_conversations_are_swept_away(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    add(api, acme_admin, bot, "Order lookup", GOOD_LOOKUP)
    fake_llm.script(GET_1002, reply("It was delivered."))
    run_tests(api, acme_admin, bot)
    with get_session_factory()() as db:
        for conversation in db.scalars(select(Conversation).where(Conversation.source == "test")):
            conversation.created_at = datetime.now(timezone.utc) - timedelta(days=5)
        db.commit()
        housekeeping.run_sweep(db)
        assert db.scalars(select(Conversation).where(Conversation.source == "test")).first() is None


# ---------- the publish gate and rollback ----------


def test_breaking_an_action_blocks_publishing_and_rollback_restores_behaviour(
    api, acme_admin, company, fake_llm
) -> None:
    bot = make_store_bot(api, acme_admin)  # version 1 is live
    add(api, acme_admin, bot, "Order lookup", GOOD_LOOKUP)

    # A change that keeps the behaviour publishes, and the version records that tests passed.
    fake_llm.script(GET_1002, reply("Your headphones were delivered 12 days ago."))
    save_draft(api, acme_admin, bot, lambda c: c.update(instructions=c["instructions"] + " Be brief."))
    ok = publish(api, acme_admin, bot)
    assert ok.status_code == 201, ok.text
    assert ok.json()["version"] == 2 and ok.json()["tests"] == {
        "passed": 1, "failed": 0, "skipped": False, "override_reason": None,
    }  # fmt: skip

    # Breaking the action's schema is caught before it reaches customers.
    save_draft(api, acme_admin, bot, break_get_order)
    fake_llm.script(GET_1002, reply("Sorry, I could not look that up."))
    blocked = publish(api, acme_admin, bot)
    assert blocked.status_code == 409
    assert "1 of 1 tests failed (Order lookup)" in blocked.json()["detail"]
    versions = api.get(f"/admin/agents/{bot.id}/versions", headers=acme_admin).json()
    assert [v["version"] for v in versions] == [2, 1]  # nothing new was published

    gate = api.get(f"/admin/agents/{bot.id}/scenario-runs?limit=1", headers=acme_admin).json()[0]
    assert (gate["purpose"], gate["failed"]) == ("publish_gate", 1)
    detail = api.get(f"/admin/agents/{bot.id}/scenario-runs/{gate['id']}", headers=acme_admin).json()
    failures = " ".join(detail["results"][0]["steps"][0]["failures"])
    assert "Expected get_order" in failures and "to be called" in failures  # a clear failing scenario

    # Skipping the tests needs a reason, and leaves a trail.
    assert publish(api, acme_admin, bot, skip_tests=True).status_code == 422
    forced = publish(api, acme_admin, bot, skip_tests=True, skip_reason="Orders API is changing on Friday")
    assert forced.status_code == 201 and forced.json()["version"] == 3
    assert forced.json()["tests"]["skipped"] is True
    assert forced.json()["tests"]["override_reason"] == "Orders API is changing on Friday"
    with get_session_factory()() as db:
        publishes = [a for a in db.scalars(select(AuditLog).where(AuditLog.action == "agent.publish"))]
        assert any("Friday" in json.dumps(a.detail) for a in publishes)

    # The broken version is live; one click brings the working one back.
    fake_llm.script(GET_1002, reply("Sorry, something went wrong."))
    assert bot.send("Where is ACM-1002?", end_user=CUSTOMER).json()["actions_taken"] == []  # refused
    assert company.calls == []

    back = api.post(f"/admin/agents/{bot.id}/versions/2/make-live", headers=acme_admin)
    assert back.status_code == 200 and back.json()["is_live"] is True
    fake_llm.script(GET_1002, reply("Your headphones were delivered 12 days ago."))
    restored = bot.send("Where is ACM-1002?", end_user=CUSTOMER).json()
    assert restored["actions_taken"] == [{"name": "get_order", "effect": "read", "ok": True}]
    assert company.calls[-1]["path"] == "/v1/orders/ACM-1002"


def test_publishing_without_tests_is_unchanged(api, acme_admin, company) -> None:
    bot = make_store_bot(api, acme_admin)
    save_draft(api, acme_admin, bot, lambda c: c.update(instructions=c["instructions"] + " Be brief."))
    published = publish(api, acme_admin, bot)
    assert published.status_code == 201
    assert published.json()["tests"] == {"passed": 0, "failed": 0, "skipped": False, "override_reason": None}


def test_a_draft_that_changes_during_the_gate_is_not_published(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    add(api, acme_admin, bot, "Order lookup", GOOD_LOOKUP)

    def edit_midway(path, payload):  # someone saves the draft while the tests are running
        fake_llm.override = None
        if path == "/chat/completions":
            save_draft(api, acme_admin, bot, lambda c: c.update(instructions="Changed while testing."))
        return fake_llm(path, payload)

    fake_llm.script(GET_1002, reply("It was delivered."))
    fake_llm.override = edit_midway
    result = publish(api, acme_admin, bot)
    assert result.status_code == 409 and "changed while the tests were running" in result.json()["detail"]


# ---------- comparing versions ----------


def test_the_diff_describes_what_changed(api, acme_admin, company) -> None:
    bot = make_store_bot(api, acme_admin)
    identical = api.get(f"/admin/agents/{bot.id}/diff?from=live&to=draft", headers=acme_admin).json()
    assert identical["identical"] is True and identical["sections"] == []

    def change(config):
        config["instructions"] = "Always greet the customer by name."
        config["actions"] = [a for a in config["actions"] if a["name"] != "create_return"]
        ticket = next(a for a in config["actions"] if a["name"] == "create_ticket")
        ticket["description"] = "Open a ticket with our team."
        config["actions"].append({**ticket, "name": "escalate", "description": "Escalate to a manager."})

    save_draft(api, acme_admin, bot, change)
    diff = api.get(f"/admin/agents/{bot.id}/diff?from=live&to=draft", headers=acme_admin).json()
    assert diff["identical"] is False
    sections = {s["title"]: s["changes"] for s in diff["sections"]}
    assert sections["Instructions"][0]["after"] == "Always greet the customer by name."
    labels = {c["label"]: c["kind"] for c in sections["Actions"]}
    assert labels["Removed action “create_return”"] == "removed"
    assert labels["New action “escalate”"] == "added"
    assert labels["Action “create_ticket”: description"] == "changed"

    edit_and_publish(api, acme_admin, bot.id, lambda c: None, note="same")  # v2 has the draft's content
    assert api.get(f"/admin/agents/{bot.id}/diff?from=live&to=draft", headers=acme_admin).json()["identical"]
    between = api.get(f"/admin/agents/{bot.id}/diff?from=1&to=2", headers=acme_admin).json()
    assert {s["title"] for s in between["sections"]} == {"Instructions", "Actions"}
    assert api.get(f"/admin/agents/{bot.id}/diff?from=1&to=99", headers=acme_admin).status_code == 404


# ---------- recording from the playground ----------


def test_a_playground_conversation_becomes_a_scenario(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    url = f"/admin/agents/{bot.id}/playground/messages"
    fake_llm.script(GET_1002, reply("Your headphones were delivered 12 days ago."))
    first = api.post(
        url, headers=acme_admin, json={"message": "Where is ACM-1002?", "mock_actions": True}
    ).json()
    session = first["session_id"]
    fake_llm.script(TICKET)
    asked = api.post(
        url,
        headers=acme_admin,
        json={"message": "Open a ticket please", "session_id": session, "mock_actions": True},
    ).json()
    assert asked["status"] == "needs_confirmation"

    saved = api.post(
        f"/admin/agents/{bot.id}/scenarios/record",
        headers=acme_admin,
        json={"session_id": session, "name": "Recorded"},
    )
    assert saved.status_code == 201, saved.text
    steps = saved.json()["definition"]["steps"]
    assert [s["say"] for s in steps] == ["Where is ACM-1002?", "Open a ticket please"]
    assert steps[0]["expect"] == {
        **steps[0]["expect"],
        "status": "completed",
        "actions_called": [{"name": "get_order", "arguments": {}}],
    }  # noqa: E501
    assert steps[1]["expect"]["status"] == "needs_confirmation"
    assert steps[1]["expect"]["actions_called"] == []  # nothing ran yet; it is waiting for a yes

    # The recording is a real, passing test.
    fake_llm.script(GET_1002, reply("Your headphones were delivered 12 days ago."), TICKET)
    result = run_tests(api, acme_admin, bot).json()
    assert (result["passed"], result["failed"]) == (1, 0), result["results"]


def test_only_your_own_playground_conversations_can_be_recorded(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    missing = api.post(
        f"/admin/agents/{bot.id}/scenarios/record",
        headers=acme_admin,
        json={"session_id": "nope", "name": "X"},
    )
    assert missing.status_code == 404
    fake_llm.script(reply("hi"))
    assert bot.send("hello", end_user=CUSTOMER, session_id="real-1").status_code == 200  # a real customer's
    stolen = api.post(
        f"/admin/agents/{bot.id}/scenarios/record",
        headers=acme_admin,
        json={"session_id": "real-1", "name": "X"},
    )
    assert stolen.status_code == 404


# ---------- replaying production runs ----------


def test_a_production_run_can_be_replayed_against_the_draft_without_calling_the_company(
    api, acme_admin, company, fake_llm
) -> None:
    bot = make_store_bot(api, acme_admin)
    fake_llm.script(GET_1002, reply("Your headphones were delivered 12 days ago."))
    original = bot.send("Where is ACM-1002?", end_user=CUSTOMER, session_id="s1").json()
    assert len(company.calls) == 1
    url = f"/admin/runs/{original['run_id']}/replay"

    # Same decisions: same behaviour, and the action is answered with what it returned the first time.
    fake_llm.script(GET_1002, reply("Your headphones were delivered 12 days ago."))
    same = api.post(url, headers=acme_admin, json={"target": "draft"}).json()
    assert same["differs"] is False and same["truncated"] is False
    (turn,) = same["turns"]
    assert turn["original"]["actions"] == turn["replayed"]["actions"] == ["get_order"]
    assert "Headphones" in json.dumps(fake_llm.decide_prompts()[-1])  # recorded result, not a live call
    assert len(company.calls) == 1

    # A configuration that now answers without looking the order up shows up as a difference.
    fake_llm.script(reply("Please check your email."))
    changed = api.post(url, headers=acme_admin, json={"target": "draft"}).json()
    assert changed["differs"] is True
    assert changed["turns"][0]["replayed"]["actions"] == []
    assert len(company.calls) == 1


def test_replay_needs_a_stored_transcript_and_the_right_workspace(
    api, acme_admin, globex_admin, company, fake_llm
) -> None:
    bot = make_store_bot(api, acme_admin, privacy={"store_transcript": "none", "retention_days": 30})
    fake_llm.script(reply("hello"))
    run = bot.send("hi", end_user=CUSTOMER, session_id="s1").json()
    url = f"/admin/runs/{run['run_id']}/replay"
    assert api.post(url, headers=acme_admin, json={}).status_code == 422
    assert api.post(url, headers=globex_admin, json={}).status_code == 404
    assert api.post(f"/admin/runs/{uuid.uuid4()}/replay", headers=acme_admin, json={}).status_code == 404

    other = make_bot(api, acme_admin)
    run2 = other.send("what is the warranty?", end_user=CUSTOMER, session_id="s2").json()
    assert (
        api.post(f"/admin/runs/{run2['run_id']}/replay", headers=acme_admin, json={"target": "9"}).status_code
        == 404
    )
