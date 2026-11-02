"""The job queue and signed webhooks: durable, retried, tenant-scoped, and SSRF-safe."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app import worker
from app.db.models import Job, WebhookDelivery
from app.db.session import get_session_factory
from app.services import jobs
from app.services import webhooks as webhook_service
from tests.company import CUSTOMER, make_store_bot
from tests.test_agent_actions import reply
from tests.test_public_api import make_bot

HOOK_URL = "https://hooks.acme.test/events"


def make_hook(api, headers, **extra):
    response = api.post("/admin/webhooks", headers=headers, json={"url": HOOK_URL, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def run_due_jobs() -> int:
    """Pretend the backoff has elapsed, then let the worker take everything that is due."""
    with get_session_factory()() as db:
        db.execute(update(Job).where(Job.status == "queued").values(run_at=datetime.now(timezone.utc)))
        db.commit()
    return worker.tick()


def deliveries(endpoint_id=None):
    with get_session_factory()() as db:
        stmt = select(WebhookDelivery).order_by(WebhookDelivery.created_at)
        if endpoint_id:
            stmt = stmt.where(WebhookDelivery.endpoint_id == endpoint_id)
        return list(db.scalars(stmt))


# ---------- the job queue ----------


@pytest.fixture
def flaky():
    """A job type whose handler fails a scripted number of times."""
    state = {"fail": 0, "ran": 0}

    @jobs.handler("test.flaky")
    def handle(db, payload):
        state["ran"] += 1
        if state["fail"] > 0:
            state["fail"] -= 1
            raise RuntimeError("boom")

    yield state
    jobs.HANDLERS.pop("test.flaky", None)


def enqueue(**kw):
    with get_session_factory()() as db:
        return jobs.enqueue(db, type="test.flaky", payload={}, **kw).id


def job_row(job_id):
    with get_session_factory()() as db:
        return db.get(Job, job_id)


def test_a_failing_job_is_retried_later_with_backoff(seeded, flaky) -> None:
    flaky["fail"] = 2
    job_id = enqueue()
    jobs.kick(job_id)  # sync mode runs it straight away
    row = job_row(job_id)
    assert (row.status, row.attempts, flaky["ran"]) == ("queued", 1, 1)
    assert row.run_at > datetime.now(timezone.utc) + timedelta(seconds=5)  # not immediately
    assert "boom" in row.last_error

    assert worker.tick() == 0  # nothing is due yet

    run_due_jobs()
    assert (job_row(job_id).status, job_row(job_id).attempts) == ("queued", 2)
    run_due_jobs()
    row = job_row(job_id)
    assert (row.status, row.attempts, row.last_error, flaky["ran"]) == ("done", 3, None, 3)


def test_a_job_that_keeps_failing_is_parked_as_failed(seeded, flaky) -> None:
    flaky["fail"] = 99
    job_id = enqueue(max_attempts=3)
    jobs.kick(job_id)
    run_due_jobs()
    run_due_jobs()
    row = job_row(job_id)
    assert (row.status, row.attempts) == ("failed", 3)
    assert run_due_jobs() == 0  # failed jobs are not picked up again


def test_a_job_left_running_by_a_dead_worker_is_picked_up_again(seeded, flaky) -> None:
    job_id = enqueue()
    with get_session_factory()() as db:
        db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status="running", locked_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        )
        db.commit()
    assert worker.tick() == 1  # the tick recovers it first, then runs it
    assert job_row(job_id).status == "done" and flaky["ran"] == 1


def test_two_workers_never_claim_the_same_job(seeded, flaky) -> None:
    job_id = enqueue()
    with get_session_factory()() as first, get_session_factory()() as second:
        claimed_first = jobs.claim(first)
        claimed_second = jobs.claim(second)
    assert claimed_first == [job_id] and claimed_second == []


def test_jobs_scheduled_for_later_wait(seeded, flaky) -> None:
    job_id = enqueue(delay_seconds=3600)
    assert worker.tick() == 0
    assert job_row(job_id).status == "queued" and flaky["ran"] == 0


def test_documents_are_indexed_through_the_queue(api, acme_admin) -> None:
    created = api.post(
        "/admin/documents",
        headers=acme_admin,
        json={"title": "Warranty", "content": "All products carry a two year warranty."},
    )
    assert created.status_code == 201
    document = api.get(f"/admin/documents/{created.json()['id']}", headers=acme_admin).json()
    assert document["status"] == "ready" and document["chunk_count"] >= 1
    with get_session_factory()() as db:
        row = db.scalars(select(Job).where(Job.type == "document.index")).one()
        assert row.status == "done" and row.payload["document_id"] == created.json()["id"]


# ---------- signing ----------


def test_signatures_verify_and_reject_tampering_and_replays() -> None:
    secret, body = "whsec_test", b'{"type":"ping"}'
    header = webhook_service.sign(secret, 1_700_000_000, body)
    assert webhook_service.verify_signature(secret, header, body, now=1_700_000_100)
    assert not webhook_service.verify_signature(secret, header, b'{"type":"pong"}', now=1_700_000_100)
    assert not webhook_service.verify_signature("whsec_other", header, body, now=1_700_000_100)
    assert not webhook_service.verify_signature(secret, header, body, now=1_700_000_000 + 3600)  # too old
    assert not webhook_service.verify_signature(secret, "garbage", body)


# ---------- endpoints ----------


def test_the_signing_secret_is_shown_once(api, acme_admin) -> None:
    created = make_hook(api, acme_admin, events=["handoff.requested"], description="Help desk")
    assert created["secret"].startswith("whsec_") and created["secret_hint"]
    listed = api.get("/admin/webhooks", headers=acme_admin).json()
    assert [e["id"] for e in listed] == [created["id"]]
    assert all("secret" not in e or e["secret"] is None for e in listed)
    assert created["secret"] not in str(listed)

    rotated = api.patch(f"/admin/webhooks/{created['id']}", headers=acme_admin, json={"rotate_secret": True})
    assert rotated.status_code == 200
    assert rotated.json()["secret"] not in (None, created["secret"])
    plain = api.patch(f"/admin/webhooks/{created['id']}", headers=acme_admin, json={"description": "x"})
    assert plain.json()["secret"] is None


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.acme.test/events",
        "https://10.0.0.5/hook",
        "https://localhost/hook",
        "ftp://hooks.acme.test/x",
    ],
)
def test_unsafe_destinations_are_refused(api, acme_admin, url) -> None:
    response = api.post("/admin/webhooks", headers=acme_admin, json={"url": url})
    assert response.status_code == 422, response.text


def test_events_must_be_known(api, acme_admin) -> None:
    bad = api.post("/admin/webhooks", headers=acme_admin, json={"url": HOOK_URL, "events": ["nope"]})
    assert bad.status_code == 422
    empty = api.post("/admin/webhooks", headers=acme_admin, json={"url": HOOK_URL, "events": []})
    assert empty.status_code == 422
    listed = api.get("/admin/webhooks/events", headers=acme_admin).json()
    assert "handoff.requested" in listed and "approval.requested" in listed


def test_webhooks_are_private_to_their_workspace(api, acme_admin, globex_admin) -> None:
    hook = make_hook(api, acme_admin)
    assert api.get("/admin/webhooks", headers=globex_admin).json() == []
    base = f"/admin/webhooks/{hook['id']}"
    assert api.patch(base, headers=globex_admin, json={"active": False}).status_code == 404
    assert api.delete(base, headers=globex_admin).status_code == 404
    assert api.post(f"{base}/test", headers=globex_admin).status_code == 404
    assert api.get(f"{base}/deliveries", headers=globex_admin).status_code == 404
    foreign_agent = api.post(
        "/admin/webhooks",
        headers=globex_admin,
        json={"url": HOOK_URL, "agent_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert foreign_agent.status_code == 422


# ---------- delivery ----------


def test_a_test_event_is_signed_and_verifiable(api, acme_admin, receiver) -> None:
    hook = make_hook(api, acme_admin)
    sent = api.post(f"/admin/webhooks/{hook['id']}/test", headers=acme_admin)
    assert sent.status_code == 200 and sent.json()["status"] == "delivered"

    (request,) = receiver.requests
    headers = request["headers"]
    assert request["path"] == "/events" and request["host"] == "hooks.acme.test"
    assert headers["x-webhook-event"] == "ping" and headers["x-webhook-id"] == request["json"]["id"]
    assert webhook_service.verify_signature(hook["secret"], headers["x-webhook-signature"], request["body"])
    assert not webhook_service.verify_signature(
        "whsec_wrong", headers["x-webhook-signature"], request["body"]
    )
    assert abs(int(headers["x-webhook-timestamp"]) - time.time()) < 60


def test_failed_deliveries_are_retried_with_the_same_event_id(api, acme_admin, receiver) -> None:
    hook = make_hook(api, acme_admin)
    receiver.statuses = [500, 503]
    first = api.post(f"/admin/webhooks/{hook['id']}/test", headers=acme_admin).json()
    assert first["status"] == "queued" and first["attempts"] == 1 and "500" in first["last_error"]

    run_due_jobs()
    run_due_jobs()
    (delivery,) = deliveries(hook["id"])
    assert (delivery.status, delivery.attempts, delivery.response_code) == ("delivered", 3, 200)
    assert len({r["json"]["id"] for r in receiver.requests}) == 1  # one event, three tries
    assert delivery.delivered_at is not None


def test_delivery_gives_up_after_the_last_attempt(api, acme_admin, receiver) -> None:
    hook = make_hook(api, acme_admin)
    receiver.statuses = [500] * 20
    api.post(f"/admin/webhooks/{hook['id']}/test", headers=acme_admin)
    for _ in range(10):
        run_due_jobs()
    (delivery,) = deliveries(hook["id"])
    assert (delivery.status, delivery.attempts) == ("failed", webhook_service.MAX_ATTEMPTS)
    assert len(receiver.requests) == webhook_service.MAX_ATTEMPTS
    log = api.get(f"/admin/webhooks/{hook['id']}/deliveries", headers=acme_admin).json()
    assert log[0]["status"] == "failed" and log[0]["last_error"]


def test_a_failed_delivery_can_be_replayed(api, acme_admin, receiver) -> None:
    hook = make_hook(api, acme_admin)
    receiver.statuses = [500] * 20
    sent = api.post(f"/admin/webhooks/{hook['id']}/test", headers=acme_admin).json()
    receiver.statuses = []  # the receiver is fixed
    replayed = api.post(
        f"/admin/webhooks/{hook['id']}/deliveries/{sent['id']}/replay", headers=acme_admin
    ).json()
    assert replayed["status"] == "delivered" and replayed["event_id"] == sent["event_id"]  # same event id


def test_a_destination_that_turns_private_is_not_contacted(api, acme_admin, receiver, public_dns) -> None:
    """DNS rebinding: the hostname was public when saved but now points inside the network."""
    hook = make_hook(api, acme_admin)
    public_dns["hooks.acme.test"] = ["10.0.0.5"]
    sent = api.post(f"/admin/webhooks/{hook['id']}/test", headers=acme_admin).json()
    assert receiver.requests == []
    assert sent["status"] == "queued" and sent["last_error"]
    assert "10.0.0.5" not in sent["last_error"]  # internal addresses are not echoed back


def test_a_disabled_endpoint_receives_nothing(api, acme_admin, receiver, fake_llm, company) -> None:
    bot = make_store_bot(api, acme_admin)
    hook = make_hook(api, acme_admin)
    api.patch(f"/admin/webhooks/{hook['id']}", headers=acme_admin, json={"active": False})
    fake_llm.script(reply("hi"))
    bot.send("hello", end_user=CUSTOMER)
    assert receiver.requests == []


# ---------- events from real traffic ----------


def test_completed_runs_are_announced_to_subscribers_only(
    api, acme_admin, receiver, fake_llm, company
) -> None:
    bot = make_store_bot(api, acme_admin)
    everything = make_hook(api, acme_admin)
    only_handoffs = make_hook(api, acme_admin, events=["handoff.requested"])
    fake_llm.script(reply("Hello there"))
    result = bot.send("hello", end_user=CUSTOMER, session_id="s1").json()

    (event,) = receiver.events("run.completed")
    assert event["data"] == {
        "run_id": result["run_id"],
        "session_id": "s1",
        "end_user_id": CUSTOMER["id"],
        "outcome": "replied",
        "reply": "Hello there",
        "agent_version": 1,
    }
    assert event["agent_id"] == bot.id and event["id"].startswith("evt_")
    assert len(deliveries(everything["id"])) == 1 and deliveries(only_handoffs["id"]) == []


def test_playground_runs_do_not_notify_production_systems(
    api, acme_admin, receiver, fake_llm, company
) -> None:
    bot = make_store_bot(api, acme_admin)
    make_hook(api, acme_admin)
    fake_llm.script(reply("test reply"))
    r = api.post(f"/admin/agents/{bot.id}/playground/messages", headers=acme_admin, json={"message": "hello"})
    assert r.status_code == 200
    assert receiver.requests == []


def test_an_endpoint_can_follow_a_single_agent(api, acme_admin, receiver, fake_llm, company) -> None:
    bot = make_store_bot(api, acme_admin)
    other = make_bot(api, acme_admin)  # the workspace's seeded knowledge agent
    make_hook(api, acme_admin, agent_id=other.id)
    fake_llm.script(reply("hi"))
    bot.send("hello", end_user=CUSTOMER)
    assert receiver.requests == []
    other.send("what is the warranty?", end_user=CUSTOMER)
    assert len(receiver.events("run.completed")) == 1


def test_one_workspaces_traffic_never_reaches_anothers_endpoint(
    api, acme_admin, globex_admin, receiver, fake_llm
) -> None:
    make_hook(api, acme_admin)
    fake_llm.script(reply("hi"))
    globex = make_bot(api, globex_admin)
    globex.send("what is the warranty?", end_user={"id": "u1"})
    assert receiver.requests == []


def test_failed_actions_are_announced(api, acme_admin, receiver, fake_llm, company) -> None:
    bot = make_store_bot(api, acme_admin)
    make_hook(api, acme_admin, events=["action.failed"])
    company.fail_status = 500
    fake_llm.script(
        {"type": "call_action", "name": "get_order", "arguments": {"order_number": "ACM-1002"}},
        reply("Sorry, I can't check that right now."),
    )
    bot.send("Where is ACM-1002?", end_user=CUSTOMER)
    (event,) = receiver.events("action.failed")
    assert event["data"]["action"] == "get_order" and event["data"]["error"] == "http_error"
