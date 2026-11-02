"""Measure retrieval quality on the golden set with the CONFIGURED model (a real key).

    python scripts/eval_retrieval.py [--mode hybrid|dense|sparse] [--rerank] [--record]

Run from the repo root with the database up and LLM_API_KEY set. It loads the 8 golden documents
(backend/tests/golden/corpus) into their own scratch workspace ("golden-eval"), asks the 32
questions (backend/tests/golden/questions.json) and prints recall@1/3/5 and MRR plus the misses.

  --record   save the numbers as backend/tests/golden/baseline_live.json
  otherwise  compare with that file and exit 1 if recall@5 or MRR got worse

The CI test (tests/test_golden_retrieval.py) runs the same documents and questions with a fake
embedder, so it guards the chunking and fusion logic. This script is the check that needs a real
model, to run before a release or after changing the chunker, embeddings or search settings.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.llm import LLMUnavailable  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import Document, Tenant, User, UserRole  # noqa: E402
from app.db.session import get_session_factory, init_db  # noqa: E402
from app.rag.evaluation import evaluate  # noqa: E402
from app.services import collections as collections_service  # noqa: E402
from app.services import documents as documents_service  # noqa: E402

GOLDEN = ROOT / "backend" / "tests" / "golden"
BASELINE = GOLDEN / "baseline_live.json"
SLUG = "golden-eval"


def scratch_workspace(db):
    tenant = db.scalars(select(Tenant).where(Tenant.slug == SLUG)).first()
    if tenant is None:
        tenant = Tenant(slug=SLUG, name="Golden evaluation")
        db.add(tenant)
        db.commit()
    user = db.scalars(select(User).where(User.tenant_id == tenant.id)).first()
    if user is None:
        user = User(
            tenant_id=tenant.id,
            email="golden@eval.local",
            password_hash=hash_password("not-a-real-login"),
            role=UserRole.admin,
            is_active=False,
        )
        db.add(user)
        db.commit()
    return tenant, user


def load_corpus(db, tenant, user):
    """Index the golden documents (unchanged ones are not re-embedded)."""
    collection = next(
        (c for c, _ in collections_service.list_collections(db, tenant_id=tenant.id) if c.name == "Golden"), None
    )
    if collection is None:
        collection = collections_service.create_collection(
            db, tenant_id=tenant.id, name="Golden", description="Retrieval evaluation"
        )
    existing = {d.title: d for d in db.scalars(select(Document).where(Document.tenant_id == tenant.id))}
    for path in sorted((GOLDEN / "corpus").glob("*.md")):
        content = path.read_text(encoding="utf-8")
        document = existing.get(path.stem)
        if document is None:
            document = documents_service.create_document(
                db, user=user, title=path.stem, source="golden", content=content, collection_id=collection.id
            )
        elif document.content != content:
            document.content = content
            db.commit()
        documents_service.process_in_session(db, document.id)
    return collection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("hybrid", "dense", "sparse"), default="hybrid")
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()

    init_db()
    cases = json.loads((GOLDEN / "questions.json").read_text(encoding="utf-8"))
    try:
        with get_session_factory()() as db:
            tenant, user = scratch_workspace(db)
            collection = load_corpus(db, tenant, user)
            result = evaluate(
                db,
                tenant_id=tenant.id,
                cases=cases,
                collection_ids=[collection.id],
                mode=args.mode,
                rerank=args.rerank,
            )
    except LLMUnavailable as exc:
        print(f"Needs working embeddings ({exc}). Set LLM_API_KEY and retry.")
        return 2

    print(f"{result['cases']} questions, mode={args.mode}, rerank={args.rerank}")
    print(f"recall@1 {result['recall']['1']:.3f}   recall@3 {result['recall']['3']:.3f}   "
          f"recall@5 {result['recall']['5']:.3f}   MRR {result['mrr']:.3f}")  # fmt: skip
    for miss in result["misses"]:
        print(f"  MISS {miss['q']!r}: expected {miss['expect']}, got {miss['got']}")

    if args.record:
        BASELINE.write_text(
            json.dumps({"cases": result["cases"], "recall": result["recall"], "mrr": result["mrr"]}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Recorded {BASELINE.relative_to(ROOT)}")
        return 0
    if not BASELINE.exists():
        print("No baseline_live.json yet. Run with --record to create it.")
        return 0
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    worse = result["recall"]["5"] < baseline["recall"]["5"] or result["mrr"] < baseline["mrr"] - 1e-9
    print(f"baseline: recall@5 {baseline['recall']['5']:.3f}   MRR {baseline['mrr']:.3f}  ->  "
          f"{'WORSE' if worse else 'ok'}")  # fmt: skip
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
