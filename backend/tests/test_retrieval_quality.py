"""Phase M: contextual passages, incremental re-indexing, query rewriting, reranking, and the
test-question quality report. The model is the deterministic fake, so these tests pin down what the
platform does with the model's answers (and what it does when they are missing or malformed)."""

import json
import uuid

from sqlalchemy import select, text

from app.core.llm import LLMUnavailable
from app.db.models import Chunk, Document, RetrievalProbe
from app.db.session import get_session_factory
from app.rag.rerank import rerank_chunks
from app.rag.retrieve import scope_key
from app.rag.sparse_search import sparse_search
from tests.test_public_api import make_bot

SECTIONS = {
    "Eligibility": "Items can be returned within 30 days of delivery if unused and in original packaging.",
    "Refund timing": "Refunds go back to the original payment method within 5 business days after we receive the item.",
    "Exceptions": "Gift cards, downloadable software and final sale items cannot be returned at all.",
    "Shipping back": "Use the prepaid label in the box and drop the parcel at any carrier location nearby.",
    "Store credit": "If you prefer, you can take store credit instead, which never expires and has no fee.",
}


def doc_text(sections=None) -> str:
    sections = SECTIONS if sections is None else sections
    return "# Returns\n\n" + "\n\n".join(f"## {name}\n{body}" for name, body in sections.items())


def upload(api, admin, title, content, **extra):
    response = api.post("/admin/documents", headers=admin, json={"title": title, "content": content, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def chunks_of(document_id):
    with get_session_factory()() as db:
        return list(
            db.scalars(
                select(Chunk).where(Chunk.document_id == uuid.UUID(document_id)).order_by(Chunk.chunk_index)
            )
        )


def unavailable_for(fake, phrase):
    """Make model calls whose system prompt contains `phrase` fail; everything else works."""

    def override(path, payload):
        if path == "/chat/completions" and phrase in payload["messages"][0]["content"]:
            raise LLMUnavailable("down")
        fake.override = None
        try:
            return fake(path, payload)
        finally:
            fake.override = override

    fake.override = override


# ---------- contextual passages ----------


def test_passages_are_stored_with_their_heading_path_and_hash(api, acme_admin) -> None:
    doc = upload(api, acme_admin, "Returns", doc_text())
    chunks = chunks_of(doc["id"])
    assert [c.context for c in chunks] == [f"Returns › {name}" for name in SECTIONS]
    assert chunks[0].content.startswith(
        "# Returns\n\n## Eligibility"
    )  # the bare title stays with its section
    assert all(c.content_hash and len(c.content_hash) == 64 for c in chunks)


def test_the_keyword_index_covers_the_heading_path(api, acme_admin) -> None:
    """ "Two years from the purchase date." never says "warranty"; its heading path does."""
    doc = upload(
        api,
        acme_admin,
        "Warranty",
        "## Coverage\nManufacturing defects are repaired free of charge during the term.\n\n## Duration\nTwo years from the purchase date.",
    )
    with get_session_factory()() as db:
        tenant_id = db.get(Document, uuid.UUID(doc["id"])).tenant_id
        found = [
            c["content"] for c in sparse_search(db, tenant_id=tenant_id, query="how long is the warranty")
        ]
        assert any("Two years" in c for c in found)
        db.execute(text("UPDATE chunks SET context = ''"))  # the generated index follows the column
        db.commit()
        found = [
            c["content"] for c in sparse_search(db, tenant_id=tenant_id, query="how long is the warranty")
        ]
        assert not any("Two years" in c for c in found)


def test_the_retrieval_tester_shows_where_a_passage_sits(api, acme_admin) -> None:
    upload(api, acme_admin, "Warranty", "## Duration\nTwo years from the purchase date.")
    hits = api.post("/admin/retrieval/test", headers=acme_admin, json={"query": "how long is the warranty"})
    assert hits.status_code == 200
    first = hits.json()["hits"][0]
    assert first["title"] == "Warranty" and first["section"] == "Warranty › Duration"
    assert first["rerank_score"] is None and hits.json()["reranked"] is False


def test_answers_cite_the_passage_not_its_heading_prefix(api, acme_admin, fake_llm) -> None:
    upload(api, acme_admin, "Warranty", "## Duration\nTwo years from the purchase date.")
    bot = make_bot(api, acme_admin)
    reply = bot.send("how long is the warranty", end_user={"id": "u1"}).json()
    assert reply["citations"] and "Warranty ›" not in reply["citations"][0]["snippet"]


# ---------- incremental re-indexing ----------


def test_editing_one_section_embeds_only_that_passage(api, acme_admin, fake_llm) -> None:
    doc = upload(api, acme_admin, "Returns", doc_text())
    assert fake_llm.embedded_texts >= len(SECTIONS)
    before = {c.context: c.content_hash for c in chunks_of(doc["id"])}
    baseline = fake_llm.embedded_texts

    edited = {
        **SECTIONS,
        "Exceptions": "Gift cards and final sale items cannot be returned, but software can.",
    }
    api.patch(f"/admin/documents/{doc['id']}", headers=acme_admin, json={"content": doc_text(edited)})
    assert fake_llm.embedded_texts - baseline == 1

    after = {c.context: c.content_hash for c in chunks_of(doc["id"])}
    changed = [name for name in before if before[name] != after[name]]
    assert changed == ["Returns › Exceptions"]


def test_renaming_a_document_re_embeds_every_passage(api, acme_admin, fake_llm) -> None:
    """The title is part of each passage's heading path, so it is part of what gets embedded."""
    doc = upload(api, acme_admin, "Returns", doc_text())
    baseline = fake_llm.embedded_texts
    api.patch(f"/admin/documents/{doc['id']}", headers=acme_admin, json={"title": "Return policy"})
    assert fake_llm.embedded_texts - baseline == len(SECTIONS)
    assert all(c.context.startswith("Return policy ›") for c in chunks_of(doc["id"]))


def test_reordering_sections_reuses_every_embedding(api, acme_admin, fake_llm) -> None:
    doc = upload(api, acme_admin, "Returns", doc_text())
    baseline = fake_llm.embedded_texts
    reordered = dict(reversed(list(SECTIONS.items())))
    api.patch(f"/admin/documents/{doc['id']}", headers=acme_admin, json={"content": doc_text(reordered)})
    # The first section moved last, so it lost its bare title heading and is the one new passage.
    assert fake_llm.embedded_texts - baseline <= 2


def test_a_reindex_with_unchanged_text_calls_the_embedding_service_zero_times(
    api, acme_admin, fake_llm
) -> None:
    doc = upload(api, acme_admin, "Returns", doc_text())
    calls = fake_llm.embed_calls
    assert api.post(f"/admin/documents/{doc['id']}/reindex", headers=acme_admin).status_code == 200
    assert fake_llm.embed_calls == calls


# ---------- query rewriting ----------


def run_steps(api, admin, run_id):
    return api.get(f"/admin/runs/{run_id}", headers=admin).json()["steps"]


def test_follow_ups_are_rewritten_into_standalone_queries(api, acme_admin, fake_llm) -> None:
    upload(api, acme_admin, "Returns", doc_text())
    bot = make_bot(api, acme_admin)
    bot.configure(lambda c: c["knowledge"].update(rewrite_queries=True))
    user = {"id": "u1"}

    first = bot.send("When do refunds arrive?", session_id="s1", end_user=user).json()
    assert fake_llm.rewrite_calls == 0  # nothing earlier to take into account

    fake_llm.rewrites.append("Can gift cards be returned?")
    second = bot.send("What about gift cards?", session_id="s1", end_user=user).json()
    assert fake_llm.rewrite_calls == 1
    steps = run_steps(api, acme_admin, second["run_id"])
    rewrite = next(s for s in steps if s["type"] == "rewrite")
    assert rewrite["input"] == {"message": "What about gift cards?"}
    assert rewrite["output"] == {"query": "Can gift cards be returned?"}
    retrieve = next(s for s in steps if s["type"] == "retrieve")
    assert retrieve["input"]["query"] == "Can gift cards be returned?"  # searched with the rewrite
    assert first["status"] == second["status"] == "completed"


def test_rewriting_is_off_unless_the_agent_asks_for_it(api, acme_admin, fake_llm) -> None:
    upload(api, acme_admin, "Returns", doc_text())
    bot = make_bot(api, acme_admin)
    bot.send("When do refunds arrive?", session_id="s1", end_user={"id": "u1"})
    bot.send("What about gift cards?", session_id="s1", end_user={"id": "u1"})
    assert fake_llm.rewrite_calls == 0


def test_a_failed_rewrite_falls_back_to_the_customers_words(api, acme_admin, fake_llm) -> None:
    upload(api, acme_admin, "Returns", doc_text())
    bot = make_bot(api, acme_admin)
    bot.configure(lambda c: c["knowledge"].update(rewrite_queries=True))
    bot.send("When do refunds arrive?", session_id="s1", end_user={"id": "u1"})

    unavailable_for(fake_llm, "standalone search query")
    answered = bot.send("What about gift cards?", session_id="s1", end_user={"id": "u1"})
    assert answered.status_code == 200
    retrieve = next(
        s for s in run_steps(api, acme_admin, answered.json()["run_id"]) if s["type"] == "retrieve"
    )
    assert retrieve["input"]["query"] == "What about gift cards?"

    fake_llm.override = None  # the outage is over; now the model answers badly instead
    for junk in ("", "x" * 400, None, 7):
        fake_llm.rewrites.append(junk)
        again = bot.send("And store credit?", session_id="s1", end_user={"id": "u1"})
        query = next(s for s in run_steps(api, acme_admin, again.json()["run_id"]) if s["type"] == "retrieve")
        assert query["input"]["query"] == "And store credit?"


# ---------- reranking ----------


def candidates():
    return [
        {
            "chunk_id": str(i),
            "document_id": "d",
            "title": f"Doc {i}",
            "content": f"passage {i}",
            "score": 1.0 / i,
        }
        for i in range(1, 6)
    ]


def test_rerank_puts_the_best_scored_passages_first(fake_llm) -> None:
    fake_llm.rerank_scores.append(
        {"scores": [{"passage": 1, "score": 2}, {"passage": 2, "score": 1}, {"passage": 3, "score": 9}]}
    )
    ranked = rerank_chunks("question", candidates(), limit=3)
    assert [c["chunk_id"] for c in ranked] == ["3", "1", "2"]  # unscored passages 4 and 5 rank last
    assert [c["rerank_score"] for c in ranked] == [9.0, 2.0, 1.0]


def test_rerank_keeps_the_original_order_when_the_model_cannot_help(fake_llm) -> None:
    for bad in ({}, {"scores": "high"}, {"scores": [{"passage": 99, "score": 5}, {"nope": 1}, "x"]}):
        fake_llm.rerank_scores.append(bad)
        assert [c["chunk_id"] for c in rerank_chunks("q", candidates(), limit=3)] == ["1", "2", "3"]
    unavailable_for(fake_llm, "rank search results")
    assert [c["chunk_id"] for c in rerank_chunks("q", candidates(), limit=2)] == ["1", "2"]


def test_rerank_clamps_scores_and_ignores_duplicates_of_nothing(fake_llm) -> None:
    fake_llm.rerank_scores.append({"scores": [{"passage": 2, "score": 500}, {"passage": 1, "score": -4}]})
    ranked = rerank_chunks("q", candidates(), limit=2)
    assert [(c["chunk_id"], c["rerank_score"]) for c in ranked] == [("2", 10.0), ("1", 0.0)]


def test_the_cache_does_not_mix_reranked_and_plain_results() -> None:
    assert scope_key(None, 0.1, 5) != scope_key(None, 0.1, 5, rerank=True)
    assert scope_key(None, 0.1, 5) == "c=all;s=0.1;k=5"  # unchanged for existing cache entries


def test_an_agent_with_reranking_answers_from_the_reranked_passages(api, acme_admin, fake_llm) -> None:
    upload(api, acme_admin, "Returns", doc_text())
    bot = make_bot(api, acme_admin)
    bot.configure(lambda c: c["knowledge"].update(rerank=True, top_k=2))
    reply = bot.send("Can I return a gift card?", end_user={"id": "u1"}).json()
    assert fake_llm.rerank_calls == 1 and reply["status"] == "completed"
    retrieve = next(s for s in run_steps(api, acme_admin, reply["run_id"]) if s["type"] == "retrieve")
    passages = retrieve["output"]["passages"]
    assert retrieve["input"]["rerank"] is True and len(passages) == 2
    assert all("rerank_score" in p for p in passages)
    assert passages[0]["rerank_score"] >= passages[1]["rerank_score"]


def test_the_retrieval_tester_can_try_reranking(api, acme_admin, fake_llm) -> None:
    upload(api, acme_admin, "Returns", doc_text())
    plain = api.post("/admin/retrieval/test", headers=acme_admin, json={"query": "gift card returns"}).json()
    fake_llm.rerank_scores.append(
        {"scores": [{"passage": n, "score": n} for n in range(1, len(plain["hits"]) + 3)]}
    )  # later candidates score higher
    reranked = api.post(
        "/admin/retrieval/test", headers=acme_admin, json={"query": "gift card returns", "rerank": True}
    ).json()
    assert reranked["reranked"] is True and fake_llm.rerank_calls == 1
    assert reranked["hits"][0]["rerank_score"] is not None
    scores = [h["rerank_score"] for h in reranked["hits"] if h["rerank_score"] is not None]
    assert scores == sorted(scores, reverse=True)


# ---------- test questions and the quality report ----------


def make_collection(api, admin, name="Help centre"):
    response = api.post("/admin/collections", headers=admin, json={"name": name, "description": ""})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def corpus(api, admin):
    cid = make_collection(api, admin)
    ids = {
        "returns": upload(api, admin, "Returns", doc_text(), collection_id=cid)["id"],
        "shipping": upload(
            api,
            admin,
            "Shipping",
            "# Shipping\n\n## Rates\nStandard delivery costs five dollars and takes about a week to arrive.\n\n"
            "## Countries\nWe ship to Canada and Mexico, and customs duties are paid by the recipient.\n\n"
            "## Tracking\nA tracking link is emailed as soon as your parcel leaves our warehouse.",
            collection_id=cid,
        )["id"],
    }
    return cid, ids


def generate(api, admin, cid, **body):
    response = api.post(f"/admin/collections/{cid}/probes/generate", headers=admin, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def report(api, admin, cid, **body):
    response = api.post(f"/admin/collections/{cid}/quality", headers=admin, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_model_writes_test_questions_from_each_documents_passages(api, acme_admin, fake_llm) -> None:
    cid, ids = corpus(api, acme_admin)
    result = generate(api, acme_admin, cid, per_document=2)
    assert result["documents"] == 2 and result["created"] == 4

    probes = api.get(f"/admin/collections/{cid}/probes", headers=acme_admin).json()
    assert len(probes) == 4 and {p["source"] for p in probes} == {"generated"}
    assert {p["document_title"] for p in probes} == {"Returns", "Shipping"}
    assert all(p["question"].startswith("Tell me about") for p in probes)

    assert generate(api, acme_admin, cid, per_document=2)["created"] == 0  # only gaps are filled
    assert generate(api, acme_admin, cid, per_document=3)["created"] == 2  # one more per document


def test_malformed_questions_from_the_model_are_ignored(api, acme_admin, fake_llm) -> None:
    cid, ids = corpus(api, acme_admin)
    fake_llm.probe_questions.append(
        {
            "questions": [
                {"passage": 1, "question": "How long do I have to return an item?"},
                {"passage": 99, "question": "Points at a passage that does not exist"},
                {"passage": 2, "question": "short"},
                {"passage": "1", "question": "The passage number is a string, not a number"},
                {"question": "No passage number at all"},
                "not even an object",
            ]
        }
    )
    fake_llm.probe_questions.append({"questions": "nope"})
    assert generate(api, acme_admin, cid, per_document=2)["created"] == 1


def test_a_report_measures_whether_the_right_document_comes_back(api, acme_admin, fake_llm) -> None:
    cid, ids = corpus(api, acme_admin)
    generate(api, acme_admin, cid, per_document=2)
    quality = report(api, acme_admin, cid)
    latest = quality["latest"]
    assert quality["previous"] is None and quality["probe_count"] == 4 and quality["document_count"] == 2
    assert latest["probe_count"] == 4 and latest["k"] == 5 and latest["rerank"] is False
    assert latest["doc_recall"] == 1.0 and latest["chunk_recall"] == 1.0 and 0.5 <= latest["mrr"] <= 1.0
    assert {row["title"] for row in latest["per_document"]} == {"Returns", "Shipping"}
    assert latest["misses"] == [] and latest["issues"] == []

    again = report(api, acme_admin, cid, k=3)
    assert again["latest"]["k"] == 3 and again["previous"]["id"] == latest["id"]
    history = api.get(f"/admin/collections/{cid}/quality/history", headers=acme_admin).json()
    assert [h["k"] for h in history] == [3, 5]


def test_a_question_the_collection_cannot_answer_shows_up_as_a_miss(api, acme_admin, fake_llm) -> None:
    cid, ids = corpus(api, acme_admin)
    generate(api, acme_admin, cid, per_document=1)
    added = api.post(
        f"/admin/collections/{cid}/probes",
        headers=acme_admin,
        json={"document_id": ids["shipping"], "question": "Quokkas juggle marmalade xylophones"},
    )
    assert added.status_code == 201 and added.json()["source"] == "manual"

    latest = report(api, acme_admin, cid)["latest"]
    assert latest["doc_recall"] < 1.0
    (miss,) = latest["misses"]
    assert miss["question"] == "Quokkas juggle marmalade xylophones" and miss["expected"] == "Shipping"
    weakest = latest["per_document"][0]
    assert weakest["title"] == "Shipping" and weakest["recall"] < 1.0  # worst documents come first


def test_the_report_points_out_documents_that_need_attention(api, acme_admin, fake_llm) -> None:
    cid, ids = corpus(api, acme_admin)
    tiny = upload(api, acme_admin, "Tiny", "Hi.", collection_id=cid)
    generate(api, acme_admin, cid, per_document=1)
    kinds = {(i["title"], i["kind"]) for i in report(api, acme_admin, cid)["latest"]["issues"]}
    assert ("Tiny", "short") in kinds and ("Tiny", "untested") in kinds
    assert not any(title in ("Returns", "Shipping") for title, _ in kinds)
    assert tiny["id"]


def test_questions_whose_passage_was_edited_away_are_dropped(api, acme_admin, fake_llm) -> None:
    cid, ids = corpus(api, acme_admin)
    generate(api, acme_admin, cid, per_document=3)
    before = [p for p in api.get(f"/admin/collections/{cid}/probes", headers=acme_admin).json()]
    shorter = {k: v for k, v in SECTIONS.items() if k not in ("Refund timing", "Store credit")}
    api.patch(f"/admin/documents/{ids['returns']}", headers=acme_admin, json={"content": doc_text(shorter)})
    generate(api, acme_admin, cid, per_document=3)
    with get_session_factory()() as db:
        hashes = {
            c.content_hash
            for c in db.scalars(select(Chunk).where(Chunk.document_id == uuid.UUID(ids["returns"])))
        }
        stale = [
            p
            for p in db.scalars(
                select(RetrievalProbe).where(RetrievalProbe.document_id == uuid.UUID(ids["returns"]))
            )
            if p.chunk_hash not in hashes
        ]
    assert stale == [] and before


def test_a_report_can_try_reranking(api, acme_admin, fake_llm) -> None:
    cid, ids = corpus(api, acme_admin)
    generate(api, acme_admin, cid, per_document=1)
    latest = report(api, acme_admin, cid, rerank=True)["latest"]
    assert latest["rerank"] is True and fake_llm.rerank_calls == 2


def test_reports_need_questions_and_knowledge_quality_is_private(
    api, acme_admin, globex_admin, fake_llm
) -> None:
    cid, ids = corpus(api, acme_admin)
    empty = api.post(f"/admin/collections/{cid}/quality", headers=acme_admin, json={})
    assert empty.status_code == 422 and "no test questions" in empty.json()["detail"]
    generate(api, acme_admin, cid, per_document=1)
    probe_id = api.get(f"/admin/collections/{cid}/probes", headers=acme_admin).json()[0]["id"]

    for method, url, body in (
        ("get", f"/admin/collections/{cid}/quality", None),
        ("post", f"/admin/collections/{cid}/quality", {}),
        ("get", f"/admin/collections/{cid}/probes", None),
        ("post", f"/admin/collections/{cid}/probes/generate", {}),
        ("post", f"/admin/collections/{cid}/reindex", None),
        ("get", f"/admin/collections/{cid}/quality/history", None),
    ):
        response = getattr(api, method)(
            url, headers=globex_admin, **({"json": body} if body is not None else {})
        )
        assert response.status_code == 404, (method, url)
    assert api.delete(f"/admin/probes/{probe_id}", headers=globex_admin).status_code == 404
    other = make_collection(api, acme_admin, "Other")
    wrong = api.post(
        f"/admin/collections/{other}/probes",
        headers=acme_admin,
        json={"document_id": ids["returns"], "question": "Is this document in the other collection?"},
    )
    assert wrong.status_code == 422  # a probe must target a document of its own collection
    assert api.delete(f"/admin/probes/{probe_id}", headers=acme_admin).status_code == 204


def test_model_outages_are_reported_not_hidden(api, acme_admin, fake_llm) -> None:
    cid, ids = corpus(api, acme_admin)
    unavailable_for(fake_llm, "write test questions")
    assert (
        api.post(f"/admin/collections/{cid}/probes/generate", headers=acme_admin, json={}).status_code == 503
    )


def test_rebuilding_a_collection_applies_new_indexing_rules(api, acme_admin, fake_llm) -> None:
    """Documents indexed before heading paths existed are brought up to date with one call."""
    cid, ids = corpus(api, acme_admin)
    with get_session_factory()() as db:
        db.execute(text("UPDATE chunks SET context = '', content_hash = NULL"))  # as before this feature
        db.commit()
    baseline = fake_llm.embedded_texts
    queued = api.post(f"/admin/collections/{cid}/reindex", headers=acme_admin)
    assert queued.status_code == 200 and queued.json() == {"documents": 2}
    assert all(c.context and c.content_hash for c in chunks_of(ids["returns"]))
    assert fake_llm.embedded_texts - baseline == len(chunks_of(ids["returns"])) + len(
        chunks_of(ids["shipping"])
    )
    assert json.dumps(chunks_of(ids["shipping"])[0].context).startswith('"Shipping')
