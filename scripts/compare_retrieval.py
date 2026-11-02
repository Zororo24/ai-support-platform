"""Show dense-only vs hybrid retrieval for keyword-heavy queries (Phase 6 evidence).

Usage (from repo root, after seeding):  python scripts/compare_retrieval.py [tenant_slug]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db.models import Tenant  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.rag.hybrid import rrf_fuse  # noqa: E402
from app.rag.retrieve import retrieve_dense  # noqa: E402
from app.rag.sparse_search import sparse_search  # noqa: E402

QUERIES = {
    "acme": ["SHIP-EXP", "Happy Return Guarantee", "Reno warehouse"],
    "globex": ["GLX-PRIO", "Flex Credit", "restocking fee"],
}


def first(chunks: list[dict]) -> str:
    if not chunks:
        return "(no results)"
    c = chunks[0]
    return f"{c['title']} :: {' '.join(c['content'].split())[:60]}..."


def main() -> None:
    slug = sys.argv[1] if len(sys.argv) > 1 else "acme"
    with get_session_factory()() as db:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == slug)).one()
        for q in QUERIES[slug]:
            dense = retrieve_dense(db, tenant_id=tenant.id, query=q, limit=20)
            sparse = sparse_search(db, tenant_id=tenant.id, query=q, limit=20)
            hybrid = rrf_fuse(dense, sparse, limit=5)
            print(f"\nquery: {q!r}")
            print(f"  dense-only top : {first(dense[:1])}")
            print(f"  sparse-only top: {first(sparse[:1])}")
            print(f"  hybrid top     : {first(hybrid)}")


if __name__ == "__main__":
    main()
