"""Agents, versions, collections and API keys (dashboard side)."""

from uuid import uuid4

from sqlalchemy import select

from app.db.models import ApiKey
from app.db.session import get_session_factory


def agents(api, headers):
    return api.get("/admin/agents", headers=headers).json()


def default_agent(api, headers):
    return agents(api, headers)[0]


def new_agent(api, headers, name="Sales bot", template="support", **extra):
    response = api.post("/admin/agents", headers=headers, json={"name": name, "template": template, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def edit_and_publish(api, headers, agent_id, mutate, note="test"):
    """Fetch the draft, apply `mutate(config)`, save it and publish."""
    config = api.get(f"/admin/agents/{agent_id}", headers=headers).json()["draft_config"]
    mutate(config)
    saved = api.put(f"/admin/agents/{agent_id}/draft", headers=headers, json=config)
    assert saved.status_code == 200, saved.text
    published = api.post(f"/admin/agents/{agent_id}/publish", headers=headers, json={"note": note})
    assert published.status_code == 201, published.text
    return published.json()


# ---------- agents ----------


def test_each_tenant_gets_a_published_default_agent(api, acme_admin, globex_admin) -> None:
    acme, globex = agents(api, acme_admin), agents(api, globex_admin)
    assert [a["name"] for a in acme] == ["Support assistant"] and acme[0]["published_version"] == 1
    assert acme[0]["has_unpublished_changes"] is False
    assert acme[0]["id"] != globex[0]["id"]
    detail = api.get(f"/admin/agents/{acme[0]['id']}", headers=acme_admin).json()
    assert len(detail["draft_config"]["knowledge"]["collection_ids"]) == 1


def test_create_agent_templates_and_slugs(api, acme_admin) -> None:
    support = new_agent(api, acme_admin, "Sales bot")
    blank = new_agent(api, acme_admin, "Sales bot", template="blank")
    assert support["draft_config"]["knowledge"]["collection_ids"]  # attached to the default collection
    assert blank["draft_config"]["knowledge"]["collection_ids"] == []
    assert support["slug"] == "sales-bot" and blank["slug"] == "sales-bot-2"
    assert support["published_version"] is None  # nothing is live until published


def test_agent_endpoints_are_admin_only_and_tenant_scoped(api, acme_user, acme_admin, globex_admin) -> None:
    agent = default_agent(api, acme_admin)
    url = f"/admin/agents/{agent['id']}"
    assert api.get("/admin/agents", headers=acme_user).status_code == 403
    assert api.get("/admin/agents").status_code == 401
    for method, path, kwargs in (
        ("get", url, {}),
        ("patch", url, {"json": {"name": "x"}}),
        ("delete", url, {}),
        ("put", f"{url}/draft", {"json": {}}),
        ("post", f"{url}/publish", {"json": {}}),
        ("get", f"{url}/versions", {}),
        ("get", f"{url}/keys", {}),
        ("post", f"{url}/keys", {"json": {"name": "k"}}),
        ("post", f"{url}/playground/messages", {"json": {"message": "hi"}}),
    ):
        assert getattr(api, method)(path, headers=globex_admin, **kwargs).status_code == 404, path
    assert default_agent(api, acme_admin)["name"] == "Support assistant"  # untouched


def test_draft_is_validated(api, acme_admin, globex_admin) -> None:
    agent = new_agent(api, acme_admin)
    url = f"/admin/agents/{agent['id']}/draft"
    globex_collection = api.get("/admin/collections", headers=globex_admin).json()[0]["id"]
    bad = [
        {"unknown_field": 1},
        {"knowledge": {"top_k": 99}},
        {"knowledge": {"min_similarity": 2}},
        {"knowledge": {"collection_ids": [str(uuid4())]}},
        {"knowledge": {"collection_ids": [globex_collection]}},  # another tenant's collection
        {"privacy": {"store_transcript": "everything"}},
        {"limits": {"max_concurrent_requests": 0}},
    ]
    for config in bad:
        response = api.put(url, headers=acme_admin, json=config)
        assert response.status_code == 422, config
    assert api.put(url, headers=acme_admin, json={"instructions": "Be brief."}).status_code == 200


def test_publish_creates_immutable_versions_and_rollback(api, acme_admin) -> None:
    agent = default_agent(api, acme_admin)
    aid = agent["id"]
    edit_and_publish(api, acme_admin, aid, lambda c: c.update(instructions="Version two"), note="second")

    versions = api.get(f"/admin/agents/{aid}/versions", headers=acme_admin).json()
    assert [(v["version"], v["is_live"], v["note"]) for v in versions] == [
        (2, True, "second"),
        (1, False, "Initial version"),
    ]
    assert versions[0]["created_by_email"] == "acme.admin@example.com"

    # Editing the draft afterwards never changes what is live.
    config = api.get(f"/admin/agents/{aid}", headers=acme_admin).json()["draft_config"]
    config["instructions"] = "Draft only"
    api.put(f"/admin/agents/{aid}/draft", headers=acme_admin, json=config)
    detail = api.get(f"/admin/agents/{aid}", headers=acme_admin).json()
    assert detail["has_unpublished_changes"] is True and detail["published_version"] == 2
    live = api.get(f"/admin/agents/{aid}/versions/2", headers=acme_admin).json()
    assert live["config"]["instructions"] == "Version two"

    rolled = api.post(f"/admin/agents/{aid}/versions/1/make-live", headers=acme_admin)
    assert rolled.status_code == 200 and rolled.json()["is_live"] is True
    assert api.get(f"/admin/agents/{aid}", headers=acme_admin).json()["published_version"] == 1
    assert api.post(f"/admin/agents/{aid}/versions/99/make-live", headers=acme_admin).status_code == 404
    assert api.get(f"/admin/agents/{aid}/versions/99", headers=acme_admin).status_code == 404


def test_delete_agent_revokes_its_keys(api, acme_admin) -> None:
    agent = new_agent(api, acme_admin)
    edit_and_publish(api, acme_admin, agent["id"], lambda c: None)
    key = api.post(f"/admin/agents/{agent['id']}/keys", headers=acme_admin, json={"name": "k"}).json()["key"]
    body = {"message": "hello"}
    auth = {"Authorization": f"Bearer {key}"}
    assert api.post(f"/v1/agents/{agent['id']}/messages", headers=auth, json=body).status_code == 200
    assert api.delete(f"/admin/agents/{agent['id']}", headers=acme_admin).status_code == 204
    assert api.post(f"/v1/agents/{agent['id']}/messages", headers=auth, json=body).status_code == 401


# ---------- API keys ----------


def test_key_secret_is_shown_once_and_only_its_hash_is_stored(api, acme_admin) -> None:
    agent = default_agent(api, acme_admin)
    created = api.post(f"/admin/agents/{agent['id']}/keys", headers=acme_admin, json={"name": "backend"})
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith("sk_live_") and body["prefix"] == body["key"][:12]

    listed = api.get(f"/admin/agents/{agent['id']}/keys", headers=acme_admin).json()
    assert listed[0]["prefix"] == body["prefix"]
    assert all("key" not in item for item in listed)

    with get_session_factory()() as db:
        row = db.scalars(select(ApiKey)).one()
        assert body["key"] not in (row.key_hash, row.prefix, row.name)
        assert len(row.key_hash) == 64


def test_revoke_key(api, acme_admin) -> None:
    agent = default_agent(api, acme_admin)
    made = api.post(f"/admin/agents/{agent['id']}/keys", headers=acme_admin, json={"name": "k"}).json()
    revoked = api.delete(f"/admin/agents/{agent['id']}/keys/{made['id']}", headers=acme_admin)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    auth = {"Authorization": f"Bearer {made['key']}"}
    assert (
        api.post(f"/v1/agents/{agent['id']}/messages", headers=auth, json={"message": "hi"}).status_code
        == 401
    )
    assert api.delete(f"/admin/agents/{agent['id']}/keys/{uuid4()}", headers=acme_admin).status_code == 404


def test_key_management_is_audited_without_secrets(api, acme_admin) -> None:
    agent = default_agent(api, acme_admin)
    made = api.post(f"/admin/agents/{agent['id']}/keys", headers=acme_admin, json={"name": "k"}).json()
    api.delete(f"/admin/agents/{agent['id']}/keys/{made['id']}", headers=acme_admin)
    entries = api.get("/admin/audit?action=api_key.", headers=acme_admin).json()["items"]
    assert {e["action"] for e in entries} == {"api_key.create", "api_key.revoke"}
    assert made["key"] not in str(entries)


# ---------- collections & documents ----------


def test_collections_crud_and_rules(api, acme_admin, globex_admin) -> None:
    listed = api.get("/admin/collections", headers=acme_admin).json()
    assert [(c["name"], c["is_default"], c["document_count"]) for c in listed] == [("General", True, 2)]

    made = api.post("/admin/collections", headers=acme_admin, json={"name": "Pricing", "description": "x"})
    assert made.status_code == 201
    cid = made.json()["id"]
    assert api.post("/admin/collections", headers=acme_admin, json={"name": "pricing"}).status_code == 409
    renamed = api.patch(f"/admin/collections/{cid}", headers=acme_admin, json={"name": "Pricing v2"})
    assert renamed.json()["name"] == "Pricing v2"

    default_id = listed[0]["id"]
    assert api.delete(f"/admin/collections/{default_id}", headers=acme_admin).status_code == 409

    doc = api.post(
        "/admin/documents", headers=acme_admin,
        json={"title": "Plans", "content": "The pro plan costs 30 dollars.", "collection_id": cid},
    ).json()  # fmt: skip
    assert doc["collection_id"] == cid and doc["collection_name"] == "Pricing v2"
    assert api.delete(f"/admin/collections/{cid}", headers=acme_admin).status_code == 409  # not empty

    in_collection = api.get(f"/admin/documents?collection_id={cid}", headers=acme_admin).json()
    assert [d["title"] for d in in_collection["items"]] == ["Plans"]

    moved = api.patch(f"/admin/documents/{doc['id']}", headers=acme_admin, json={"collection_id": default_id})
    assert moved.json()["collection_name"] == "General"
    assert api.delete(f"/admin/collections/{cid}", headers=acme_admin).status_code == 204

    assert api.get("/admin/collections", headers=globex_admin).json()[0]["document_count"] == 2
    assert (
        api.patch(f"/admin/collections/{default_id}", headers=globex_admin, json={"name": "x"}).status_code
        == 404
    )


def test_documents_default_to_the_default_collection_and_reject_foreign_ones(
    api, acme_admin, globex_admin
) -> None:
    doc = api.post(
        "/admin/documents", headers=acme_admin, json={"title": "T", "content": "hello world"}
    ).json()
    assert doc["collection_name"] == "General"
    foreign = api.get("/admin/collections", headers=globex_admin).json()[0]["id"]
    bad = api.post(
        "/admin/documents", headers=acme_admin,
        json={"title": "U", "content": "other text", "collection_id": foreign},
    )  # fmt: skip
    assert bad.status_code == 422
    upload = api.post(
        "/admin/documents/upload",
        headers=acme_admin,
        files={"file": ("n.txt", b"notes")},
        data={"collection_id": foreign},
    )
    assert upload.status_code == 422
