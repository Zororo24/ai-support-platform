"""Retrieval quality on a fixed golden set: a change that makes search worse fails here.

The numbers come from the *fake* embedding model (a hashed bag of words), so they measure the
chunking, heading context and fusion logic, not semantic understanding. The same questions and
documents are run against a real model with `scripts/eval_retrieval.py` before a release.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Document, User
from app.db.session import get_session_factory
from app.rag.evaluation import evaluate
from app.services import collections as collections_service
from app.services import documents as documents_service

GOLDEN = Path(__file__).parent / "golden"
BASELINE = GOLDEN / "baseline_fake.json"
FLOOR_RECALL_AT_5 = 0.85


def load_cases() -> list[dict[str, str]]:
    return json.loads((GOLDEN / "questions.json").read_text(encoding="utf-8"))


@pytest.fixture
def golden(seeded):
    """The golden documents in their own collection of the Acme workspace."""
    with get_session_factory()() as db:
        admin = db.scalars(select(User).where(User.email == "acme.admin@example.com")).one()
        collection = collections_service.create_collection(
            db, tenant_id=admin.tenant_id, name="Golden", description=""
        )
        for path in sorted((GOLDEN / "corpus").glob("*.md")):
            document = documents_service.create_document(
                db,
                user=admin,
                title=path.stem,
                source="golden",
                content=path.read_text(encoding="utf-8"),
                collection_id=collection.id,
            )
            documents_service.process_in_session(db, document.id)
        yield db, admin.tenant_id, collection.id


def test_every_golden_document_is_indexed(golden) -> None:
    db, tenant_id, _ = golden
    documents = list(db.scalars(select(Document).where(Document.source == "golden")))
    assert len(documents) == 8 and all(d.status.value == "ready" and d.chunk_count >= 1 for d in documents)


def test_recall_does_not_fall_below_the_recorded_baseline(golden) -> None:
    db, tenant_id, collection_id = golden
    result = evaluate(db, tenant_id=tenant_id, cases=load_cases(), collection_ids=[collection_id])
    print(
        "\ngolden hybrid:", json.dumps({k: result[k] for k in ("recall", "mrr")}), "misses:", result["misses"]
    )
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert result["recall"]["5"] >= FLOOR_RECALL_AT_5, result["misses"]
    for k in ("1", "3", "5"):
        assert result["recall"][k] >= baseline["recall"][k], (
            k,
            result["recall"],
            baseline["recall"],
            result["misses"],
        )
    assert result["mrr"] >= baseline["mrr"] - 1e-9


def test_hybrid_is_no_worse_than_either_search_alone(golden) -> None:
    db, tenant_id, collection_id = golden
    cases = load_cases()
    scores = {
        mode: evaluate(db, tenant_id=tenant_id, cases=cases, collection_ids=[collection_id], mode=mode)
        for mode in ("hybrid", "dense", "sparse")
    }
    print("\n", {m: (r["recall"], r["mrr"]) for m, r in scores.items()})
    assert scores["hybrid"]["recall"]["5"] >= max(
        scores["dense"]["recall"]["5"], scores["sparse"]["recall"]["5"]
    )
