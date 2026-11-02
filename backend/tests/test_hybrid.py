from unittest.mock import patch

from sqlalchemy import select

from app.db.models import Tenant
from app.db.session import get_session_factory
from app.rag.hybrid import rrf_fuse
from app.rag.retrieve import retrieve
from app.rag.sparse_search import build_or_query, sparse_search


def item(cid: str) -> dict:
    return {"chunk_id": cid, "document_id": "d", "title": "t", "content": cid, "score": 0}


def test_rrf_rewards_agreement_between_lists() -> None:
    fused = rrf_fuse([item("a"), item("b"), item("c")], [item("c"), item("b"), item("d")], limit=4)
    # b and c appear in both lists, so they beat a and d; c (ranks 3,1) edges b (2,2).
    assert [f["chunk_id"] for f in fused][:2] == ["c", "b"]
    assert {f["chunk_id"] for f in fused} == {"a", "b", "c", "d"}


def test_rrf_scores_match_formula() -> None:
    fused = rrf_fuse([item("a")], [item("a")], k=8)
    assert abs(fused[0]["score"] - 2 / 9) < 1e-9


def test_rrf_limit_and_empty() -> None:
    assert rrf_fuse([], []) == []
    assert len(rrf_fuse([item(str(i)) for i in range(10)], limit=3)) == 3


def test_or_query_building() -> None:
    assert build_or_query("What is SHIP-EXP?") == "what | is | ship | exp"
    assert build_or_query("???") == ""


def _tenant(db, slug):
    return db.scalars(select(Tenant).where(Tenant.slug == slug)).one()


def test_keyword_query_hits_via_sparse_and_hybrid(seeded) -> None:
    """A SKU-style token: FTS pins the exact doc; fused result includes it."""
    with get_session_factory()() as db:
        acme = _tenant(db, "acme")
        sparse = sparse_search(db, tenant_id=acme.id, query="SHIP-EXP")
        assert sparse and sparse[0]["title"] == "Acme Shipping FAQ"
        chunks, _ = retrieve(db, tenant_id=acme.id, query="SHIP-EXP")
        assert chunks[0]["title"] == "Acme Shipping FAQ"


def test_sparse_and_dense_are_tenant_scoped(seeded) -> None:
    with get_session_factory()() as db:
        acme = _tenant(db, "acme")
        # GLX-PRIO / Flex Credit exist only in Globex docs.
        assert sparse_search(db, tenant_id=acme.id, query="GLX-PRIO Flex Credit Rotterdam") == []
        chunks, _ = retrieve(db, tenant_id=acme.id, query="Globex Flex Credit")
        assert all("Globex" not in c["title"] for c in chunks)


def test_cache_hit_skips_database(seeded) -> None:
    with get_session_factory()() as db:
        acme = _tenant(db, "acme")
        first, hit1 = retrieve(db, tenant_id=acme.id, query="return window")
        assert hit1 is False
        # If the second call touched the DB layer these would blow up.
        with (
            patch("app.rag.retrieve.sparse_search", side_effect=AssertionError("db touched")),
            patch("app.rag.retrieve.retrieve_dense", side_effect=AssertionError("db touched")),
        ):
            second, hit2 = retrieve(db, tenant_id=acme.id, query="return window")
        assert hit2 is True
        assert second == first


def test_cache_is_tenant_partitioned(seeded) -> None:
    with get_session_factory()() as db:
        acme, globex = _tenant(db, "acme"), _tenant(db, "globex")
        a, _ = retrieve(db, tenant_id=acme.id, query="return window")
        g, hit = retrieve(db, tenant_id=globex.id, query="return window")
        assert hit is False
        assert a != g
        assert all("Globex" in c["title"] for c in g)


def test_chat_reports_cache_hit_and_ingest_invalidates(api, acme_user, acme_admin) -> None:
    def ask():
        return api.post("/chat/messages", headers=acme_user, json={"content": "return window"}).json()

    assert ask()["cache_hit"] is False
    assert ask()["cache_hit"] is True
    api.post(
        "/admin/documents", headers=acme_admin, json={"title": "New", "content": "return window extended"}
    )
    assert ask()["cache_hit"] is False
