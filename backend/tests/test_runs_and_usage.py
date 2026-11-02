"""Runs list, run traces, usage totals, and per-agent insights."""

import json

from tests.company import CUSTOMER, make_store_bot
from tests.test_agent_actions import GET_1002, TICKET, reply
from tests.test_agents_platform import default_agent
from tests.test_public_api import GOOD, make_bot


def runs(api, headers, query=""):
    r = api.get(f"/admin/runs{query}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- list ----------


def test_runs_list_summarises_each_turn(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    fake_llm.script(GET_1002, reply("Your headphones arrived 12 days ago."))
    bot.send("Where is ACM-1002?", end_user=CUSTOMER, session_id="web-1")
    faq = make_bot(api, acme_admin, default_agent(api, acme_admin))
    faq.send(GOOD, session_id="web-2", end_user={"id": "u2"})
    listing = runs(api, acme_admin)
    assert listing["total"] >= 1
    first = next(r for r in listing["items"] if r["session_id"] == "web-1")
    assert (
        first["agent_name"] == "Store bot"
        and first["source"] == "api"
        and first["end_user_ref"] == "web-user-1"
    )
    assert (first["status"], first["outcome"], first["actions"]) == ("completed", "replied", 1)
    assert first["input_preview"] == "Where is ACM-1002?" and "12 days" in first["reply_preview"]
    assert first["tokens"] > 0 and first["duration_ms"] is not None and first["agent_version"] == 1
    started = [r["started_at"] for r in listing["items"]]
    assert started == sorted(started, reverse=True)  # newest first


def test_run_filters(api, acme_admin, company, fake_llm) -> None:
    store = make_store_bot(api, acme_admin)
    faq = make_bot(api, acme_admin, default_agent(api, acme_admin))
    fake_llm.script(TICKET)
    store.send("open a ticket", end_user=CUSTOMER, session_id="w1")  # left waiting
    fake_llm.script(reply("hi"))
    store.send("hello", end_user={"id": "other", "attributes": {"customer_id": "c9"}}, session_id="w2")
    faq.send(GOOD)

    assert {r["session_id"] for r in runs(api, acme_admin, f"?agent_id={store.id}")["items"]} == {"w1", "w2"}
    waiting = runs(api, acme_admin, "?status=waiting_confirmation")["items"]
    assert [r["session_id"] for r in waiting] == ["w1"] and waiting[0]["outcome"] == "needs_confirmation"
    assert [r["session_id"] for r in runs(api, acme_admin, "?end_user=other")["items"]] == ["w2"]
    assert runs(api, acme_admin, "?source=playground")["items"] == []
    page = runs(api, acme_admin, "?limit=1")
    assert len(page["items"]) == 1 and page["total"] == 3
    assert api.get("/admin/runs?status=bogus", headers=acme_admin).status_code == 422


def test_playground_runs_are_marked_as_such(api, acme_admin, fake_llm) -> None:
    agent = default_agent(api, acme_admin)
    api.post(f"/admin/agents/{agent['id']}/playground/messages", headers=acme_admin, json={"message": GOOD})
    (run,) = runs(api, acme_admin)["items"]
    assert run["source"] == "playground" and run["outcome"] == "answered"
    assert runs(api, acme_admin, "?source=api")["items"] == []


# ---------- detail / trace ----------


def test_run_detail_has_the_full_ordered_trace(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    fake_llm.script(GET_1002, reply("Delivered."))
    run_id = bot.send("where is it", end_user=CUSTOMER).json()["run_id"]
    detail = api.get(f"/admin/runs/{run_id}", headers=acme_admin).json()
    assert [(s["seq"], s["type"], s["name"]) for s in detail["steps"]] == [
        (1, "llm", "call_action"),
        (2, "action", "get_order"),
        (3, "llm", "reply"),
    ]
    action = detail["steps"][1]
    assert (
        action["input"]["arguments"] == {"order_number": "ACM-1002"} and action["input"]["effect"] == "read"
    )
    assert action["output"]["ok"] is True and action["output"]["status_code"] == 200
    assert action["status"] == "ok" and action["latency_ms"] >= 0
    assert "acme-secret" not in json.dumps(detail) and "cust_1" not in json.dumps(detail["steps"][0])


def test_waiting_run_shows_what_it_is_waiting_for_then_the_rest_of_the_story(
    api, acme_admin, company, fake_llm
) -> None:
    bot = make_store_bot(api, acme_admin)
    fake_llm.script(TICKET)
    first = bot.send("open a ticket for my headphones", end_user=CUSTOMER, session_id="s1").json()
    waiting = api.get(f"/admin/runs/{first['run_id']}", headers=acme_admin).json()
    assert waiting["status"] == "waiting_confirmation"
    assert waiting["pending"]["action"] == "create_ticket" and waiting["pending"]["summary"].startswith(
        "Open a support ticket"
    )
    assert [s["type"] for s in waiting["steps"]] == ["llm", "confirm"]

    fake_llm.script(reply("Opened."))
    bot.send("yes", end_user=CUSTOMER, session_id="s1")
    done = api.get(f"/admin/runs/{first['run_id']}", headers=acme_admin).json()
    assert done["status"] == "completed" and done["pending"] is None
    assert [(s["type"], s["name"]) for s in done["steps"]] == [
        ("llm", "call_action"), ("confirm", "create_ticket"), ("confirm", "classified"), ("action", "create_ticket"), ("llm", "reply"),
    ]  # fmt: skip
    assert (
        done["input_preview"] == "open a ticket for my headphones"
    )  # the original request stays the headline


def test_failed_runs_are_listed_with_their_error_and_steps(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    fake_llm.script({"type": "dance"}, {"type": "dance"})
    assert bot.send("hello", end_user=CUSTOMER).status_code == 503
    (failed,) = runs(api, acme_admin, "?status=failed")["items"]
    assert "invalid step" in failed["error"]
    detail = api.get(f"/admin/runs/{failed['id']}", headers=acme_admin).json()
    assert detail["steps"][-1]["name"] == "invalid_decision" and detail["steps"][-1]["status"] == "error"


def test_runs_are_admin_only_and_tenant_scoped(
    api, acme_user, acme_admin, globex_admin, company, fake_llm
) -> None:
    bot = make_store_bot(api, acme_admin)
    fake_llm.script(reply("hi"))
    run_id = bot.send("hello", end_user=CUSTOMER).json()["run_id"]
    assert api.get("/admin/runs", headers=acme_user).status_code == 403
    assert api.get("/admin/runs").status_code == 401
    assert runs(api, globex_admin)["items"] == [] and runs(api, globex_admin)["total"] == 0
    assert api.get(f"/admin/runs/{run_id}", headers=globex_admin).status_code == 404
    assert api.get("/admin/runs/00000000-0000-0000-0000-000000000000", headers=acme_admin).status_code == 404
    assert api.get("/admin/usage", headers=acme_user).status_code == 403


# ---------- usage ----------


def test_usage_totals_series_and_per_agent_breakdown(
    api, acme_admin, globex_admin, company, fake_llm
) -> None:
    bot = make_store_bot(api, acme_admin)
    faq = make_bot(api, acme_admin, default_agent(api, acme_admin))
    fake_llm.script(GET_1002, reply("ok"), {"type": "dance"}, {"type": "dance"})
    bot.send("check", end_user=CUSTOMER)
    bot.send("breaks", end_user=CUSTOMER)  # fails: two malformed decisions
    faq.send(GOOD)

    usage = api.get("/admin/usage?days=7", headers=acme_admin).json()
    assert usage["days"] == 7 and len(usage["series"]) == 7
    assert (
        usage["totals"]["runs"] == 3
        and usage["totals"]["failed"] == 1
        and usage["totals"]["action_calls"] == 1
    )
    assert usage["totals"]["tokens"] > 0
    today = usage["series"][-1]
    assert (
        today["runs"] == 3
        and today["action_calls"] == 1
        and all(d["runs"] == 0 for d in usage["series"][:-1])
    )
    by_agent = {a["name"]: a for a in usage["by_agent"]}
    assert (
        by_agent["Store bot"]["runs"] == 2
        and by_agent["Store bot"]["failed"] == 1
        and by_agent["Store bot"]["action_calls"] == 1
    )
    assert by_agent["Support assistant"]["runs"] == 1
    assert usage["by_agent"][0]["name"] == "Store bot"  # busiest first

    other = api.get("/admin/usage", headers=globex_admin).json()
    assert other["totals"] == {"runs": 0, "tokens": 0, "action_calls": 0, "failed": 0}
    assert api.get("/admin/usage?days=0", headers=acme_admin).status_code == 422


# ---------- insights per agent ----------


def test_insights_can_be_filtered_to_one_agent(api, acme_admin, company, fake_llm) -> None:
    store = make_store_bot(api, acme_admin)
    faq = make_bot(api, acme_admin, default_agent(api, acme_admin))
    fake_llm.script({"type": "answer_from_knowledge", "question": "zzzqx plugh xyzzy"})
    store.send("zzzqx plugh xyzzy", end_user=CUSTOMER)  # a refusal by the store agent
    faq.send(GOOD)  # an answer by the FAQ agent

    everything = api.get("/admin/insights/summary", headers=acme_admin).json()
    only_store = api.get(f"/admin/insights/summary?agent_id={store.id}", headers=acme_admin).json()
    only_faq = api.get(f"/admin/insights/summary?agent_id={faq.id}", headers=acme_admin).json()
    assert everything["replies"] == 2
    assert only_store["outcomes"] == {"refused": 1} and only_faq["outcomes"] == {"answered": 1}

    gaps_all = api.get("/admin/insights/gaps", headers=acme_admin).json()
    gaps_faq = api.get(f"/admin/insights/gaps?agent_id={faq.id}", headers=acme_admin).json()
    assert [g["question"] for g in gaps_all] == ["zzzqx plugh xyzzy"] and gaps_faq == []
