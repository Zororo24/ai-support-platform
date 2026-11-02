"""The public API contract (a committed snapshot), the thin Python and Node clients, and the sample site.

Run with UPDATE_SNAPSHOTS=1 to rewrite the snapshot after an intended, reviewed change to /v1.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import update

from app.db.models import Run
from app.db.session import get_session_factory
from app.main import create_app
from app.services import webhooks as webhook_service
from tests.company import make_store_bot
from tests.test_agent_actions import GET_1002, TICKET, reply
from tests.test_handoff_handback_approvals import REFUND_CALL, handback_bot
from tests.test_public_api import make_bot

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = Path(__file__).parent / "snapshots" / "openapi_v1.json"
sys.path.insert(0, str(ROOT / "clients" / "python"))

from agent_platform import AgentPlatform, AgentPlatformError, Reply, verify_webhook  # noqa: E402

CUSTOMER = {"end_user_id": "web-user-1", "end_user_attributes": {"customer_id": "cust_1"}}


# ---------- the contract ----------


def v1_contract() -> dict:
    """Everything a client of /v1 can depend on: the paths and the schemas they refer to."""
    spec = create_app().openapi()
    paths = {path: item for path, item in spec["paths"].items() if path.startswith("/v1")}
    schemas = spec.get("components", {}).get("schemas", {})
    needed: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.rsplit("/", 1)[-1]
                if name not in needed:
                    needed.add(name)
                    walk(schemas[name])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(paths)
    return {"paths": paths, "schemas": {name: schemas[name] for name in sorted(needed)}}


def test_the_public_api_matches_its_committed_contract() -> None:
    current = json.dumps(v1_contract(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(current, encoding="utf-8")
        pytest.skip("snapshot rewritten")
    assert SNAPSHOT.exists(), "run once with UPDATE_SNAPSHOTS=1 to create tests/snapshots/openapi_v1.json"
    assert current == SNAPSHOT.read_text(encoding="utf-8"), (
        "The /v1 contract changed. /v1 only evolves additively: adding fields or endpoints is fine, "
        "removing or changing the meaning of anything breaks customers. If this change is intended and "
        "additive, rewrite the snapshot with UPDATE_SNAPSHOTS=1 and review the diff."
    )


def test_the_contract_only_covers_the_public_api() -> None:
    contract = v1_contract()
    assert contract["paths"] and all(p.startswith("/v1/agents/") for p in contract["paths"])
    assert {"AgentMessageIn", "AgentMessageOut", "ToolResultIn", "ConfirmationIn"} <= set(contract["schemas"])


# ---------- the Python client ----------


def client_for(api, bot):
    return AgentPlatform(api_key=bot.key, agent_id=bot.id, http=api)


def test_send_returns_a_typed_reply(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    agents = client_for(api, bot)
    fake_llm.script(GET_1002, reply("Your headphones were delivered 12 days ago."))
    result = agents.send("Where is ACM-1002?", session_id="c1", **CUSTOMER)
    assert isinstance(result, Reply) and result.status == "completed" and result.session_id == "c1"
    assert "12 days" in result.reply and result.actions_taken[0]["name"] == "get_order"
    assert result.raw["usage"]["tokens"] > 0
    assert company.calls[0]["query"] == {"customer_id": "cust_1"}  # the attributes the client sent


def test_confirmation_round_trip(api, acme_admin, company, fake_llm) -> None:
    bot = make_store_bot(api, acme_admin)
    agents = client_for(api, bot)
    fake_llm.script(TICKET)
    asked = agents.send("Open a ticket for my headphones", session_id="c1", **CUSTOMER)
    assert asked.status == "needs_confirmation" and asked.pending_confirmation["id"]
    fake_llm.script(reply("Done."))
    done = agents.confirm("c1", asked.pending_confirmation["id"], approve=True)
    assert done.status == "completed" and len(company.posts()) == 1

    fake_llm.script(TICKET)
    asked = agents.send("Open another ticket", session_id="c2", **CUSTOMER)
    declined = agents.confirm("c2", asked.pending_confirmation["id"], approve=False)
    assert declined.status == "completed" and len(company.posts()) == 1


def test_hand_back_round_trip(api, acme_admin, company, fake_llm) -> None:
    bot = handback_bot(api, acme_admin)
    agents = client_for(api, bot)
    fake_llm.script(REFUND_CALL)
    waiting = agents.send("Refund ACM-1002", session_id="c1", **CUSTOMER)
    assert waiting.status == "action_required" and waiting.action_request["name"] == "issue_refund"

    with pytest.raises(AgentPlatformError) as blocked:
        agents.send("hello?", session_id="c1", **CUSTOMER)
    assert blocked.value.code == "awaiting_tool_result" and blocked.value.status_code == 409

    fake_llm.script(reply("Refund issued."))
    done = agents.send_tool_result("c1", waiting.action_request["call_id"], result={"refund_id": "RF-1"})
    assert done.status == "completed"

    fake_llm.script(REFUND_CALL, reply("Sorry, that failed."))
    again = agents.send("Refund ACM-1002 again", session_id="c1", **CUSTOMER)
    failed = agents.send_tool_result("c1", again.action_request["call_id"], error="card already refunded")
    assert failed.actions_taken[0]["ok"] is False


def test_errors_carry_the_apis_code_and_retry_hint(api, acme_admin) -> None:
    bot = make_bot(api, acme_admin)
    with pytest.raises(AgentPlatformError) as bad:
        AgentPlatform(api_key="sk_live_nope", agent_id=bot.id, http=api).send("hi")
    assert (bad.value.status_code, bad.value.code) == (
        401,
        "invalid_api_key",
    ) and bad.value.retry_after is None

    agents = client_for(api, bot)
    with pytest.raises(AgentPlatformError) as missing:
        agents.session("never-existed")
    assert missing.value.code == "session_not_found"

    bot.configure(lambda c: c["limits"].update(daily_token_budget=1000))
    agents.send("hello", **CUSTOMER)
    with get_session_factory()() as db:
        db.execute(update(Run).values(tokens=5000))
        db.commit()
    with pytest.raises(AgentPlatformError) as over:
        agents.send("hello again", **CUSTOMER)
    assert over.value.code == "budget_exceeded" and over.value.retry_after and over.value.retry_after > 0


def test_streaming_yields_progress_then_the_answer(api, acme_admin) -> None:
    bot = make_bot(api, acme_admin)
    events = list(client_for(api, bot).stream("what is the return policy?", **CUSTOMER))
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "done" and "stage" in kinds[:-1]
    done = events[-1][1]
    assert done["status"] == "completed" and done["reply"]
    with pytest.raises(AgentPlatformError):
        list(AgentPlatform(api_key="sk_live_nope", agent_id=bot.id, http=api).stream("hi"))


def test_retries_with_an_idempotency_key_return_the_first_answer(api, acme_admin) -> None:
    agents = client_for(api, make_bot(api, acme_admin))
    first = agents.send("what is the return policy?", session_id="c1", idempotency_key="order-1", **CUSTOMER)
    second = agents.send("what is the return policy?", session_id="c1", idempotency_key="order-1", **CUSTOMER)
    assert first.run_id == second.run_id


def test_a_customers_data_can_be_erased(api, acme_admin) -> None:
    agents = client_for(api, make_bot(api, acme_admin))
    for session in ("a", "b"):
        agents.send("hello", session_id=session, **CUSTOMER)
    assert agents.session("a")["message_count"] >= 2
    assert agents.delete_end_user("web-user-1") == 2
    with pytest.raises(AgentPlatformError):
        agents.session("a")
    agents.send("hello", session_id="c", **CUSTOMER)
    agents.delete_session("c")
    with pytest.raises(AgentPlatformError):
        agents.session("c")


# ---------- verifying webhooks (Python and Node agree) ----------


def signed(secret: str, body: bytes, age: int = 0) -> str:
    return webhook_service.sign(secret, int(time.time()) - age, body)


def test_verify_webhook_accepts_only_fresh_untouched_deliveries_signed_with_the_secret() -> None:
    body = b'{"id":"evt_1","type":"ping"}'
    header = signed("whsec_abc", body)
    assert verify_webhook("whsec_abc", header, body)
    assert not verify_webhook("whsec_other", header, body)
    assert not verify_webhook("whsec_abc", header, body + b" ")
    assert not verify_webhook("whsec_abc", signed("whsec_abc", body, age=3600), body)
    assert verify_webhook("whsec_abc", signed("whsec_abc", body, age=3600), body, tolerance=7200)
    for junk in ("", "garbage", "t=abc,v1=00", "v1=00"):
        assert not verify_webhook("whsec_abc", junk, body)


def node_path() -> str | None:
    return shutil.which("node") or next(
        (p for p in ("C:/Program Files/nodejs/node.exe", "/usr/local/bin/node") if Path(p).exists()), None
    )


@pytest.mark.skipif(node_path() is None, reason="Node is not installed")
def test_the_node_client_verifies_what_the_platform_signs() -> None:
    module = (ROOT / "clients" / "node" / "agent-platform.mjs").as_uri()
    script = (
        f'import {{ verifyWebhook }} from "{module}";'
        "const [secret, header, old, body] = process.argv.slice(1);"
        "console.log(JSON.stringify([verifyWebhook(secret, header, body), verifyWebhook('other', header, body),"
        " verifyWebhook(secret, header, body + 'x'), verifyWebhook(secret, old, body), verifyWebhook(secret, 'junk', body)]));"
    )
    body = '{"id":"evt_1","type":"ping","data":{"note":"héllo ✓"}}'
    header = signed("whsec_abc", body.encode())
    old = signed("whsec_abc", body.encode(), age=3600)
    out = subprocess.run(
        [node_path(), "--input-type=module", "-e", script, "whsec_abc", header, old, body],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [True, False, False, False, False]


# ---------- the sample site ----------


def load_site():
    spec = importlib.util.spec_from_file_location(
        "chat_site_server", ROOT / "examples" / "chat-site" / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StubAgents:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def send_tool_result(self, session_id, call_id, result=None, error=None):
        self.sent.append({"session": session_id, "call": call_id, "result": result, "error": error})
        return self.replies.pop(0)


def action_required(name="issue_refund"):
    return Reply(
        status="action_required",
        reply="One moment",
        session_id="s1",
        action_request={"call_id": "call_1", "name": name, "arguments": {"order_number": "ACM-1002"}},
    )


def test_the_sample_site_performs_requested_actions_and_reports_the_outcome() -> None:
    site = load_site()
    site.agents = StubAgents([Reply(status="completed", reply="Refunded.", session_id="s1")])
    final = site.finish_actions(action_required())
    assert final.reply == "Refunded." and site.agents.sent[0]["result"]["status"] == "issued"
    assert site.agents.sent[0]["error"] is None


def test_the_sample_site_reports_an_action_it_cannot_do_instead_of_hanging() -> None:
    site = load_site()
    site.agents = StubAgents([Reply(status="completed", reply="Sorry.", session_id="s1")])
    final = site.finish_actions(action_required("teleport"))
    assert final.reply == "Sorry." and "cannot do teleport" in site.agents.sent[0]["error"]


def test_the_sample_site_describes_replies_for_the_browser() -> None:
    site = load_site()
    asking = Reply(
        status="needs_confirmation", reply="Refund?", session_id="s1", pending_confirmation={"id": "conf_1"}
    )
    shown = site.present(asking)
    assert shown["confirmation"] == {"id": "conf_1"} and shown["status"] == "needs_confirmation"
    assert (
        site.present(Reply(status="handed_off", reply="Passing you on", session_id="s1", handoff={}))[
            "handoff"
        ]
        is True
    )
