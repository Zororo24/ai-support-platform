import io

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.llm import LLMUnavailable
from app.db.models import Chunk
from app.db.session import get_session_factory


def make_pdf(text: str) -> bytes:
    """A minimal single-page PDF whose page contains `text` (valid xref, Helvetica)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def make_docx(heading: str, body: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading(heading, level=1)
    doc.add_paragraph(body)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def upload(api, headers, name, data, title=None):
    form = {"title": title} if title else {}
    return api.post("/admin/documents/upload", headers=headers, files={"file": (name, data)}, data=form)


def detail(api, headers, doc_id):
    return api.get(f"/admin/documents/{doc_id}", headers=headers).json()


def ask(api, headers, text):
    return api.post("/chat/messages", headers=headers, json={"content": text}).json()


def chunk_total(tenant_slug=None) -> int:
    with get_session_factory()() as db:
        return db.scalar(select(func.count()).select_from(Chunk)) or 0


# ---------- upload & extraction ----------


def test_markdown_upload_is_indexed_and_answerable(api, acme_admin, acme_user) -> None:
    md = b"# Acme Warranty\nAll Acme gadgets carry a 2 year warranty."
    created = upload(api, acme_admin, "warranty.md", md)
    assert created.status_code == 201
    assert created.json()["status"] == "pending"
    assert created.json()["title"] == "warranty"
    d = detail(api, acme_admin, created.json()["id"])
    assert d["status"] == "ready" and d["chunk_count"] == len(d["chunks"]) >= 1
    assert d["file_name"] == "warranty.md" and d["created_by_email"] == "acme.admin@example.com"
    assert "2 year" in ask(api, acme_user, "How long is the gadget warranty?")["content"]


def test_pdf_upload_extracts_text(api, acme_admin) -> None:
    created = upload(api, acme_admin, "policy.pdf", make_pdf("Gadget repairs take 10 business days"))
    d = detail(api, acme_admin, created.json()["id"])
    assert d["status"] == "ready", d
    assert "10 business days" in d["content"]


def test_docx_upload_extracts_text_and_headings(api, acme_admin) -> None:
    created = upload(api, acme_admin, "faq.docx", make_docx("Gift wrapping", "Wrapping costs five dollars."))
    d = detail(api, acme_admin, created.json()["id"])
    assert d["status"] == "ready", d
    assert "# Gift wrapping" in d["content"] and "five dollars" in d["content"]


def test_custom_title_is_used(api, acme_admin) -> None:
    created = upload(api, acme_admin, "x.txt", b"Plain text about lanyards.", title="Lanyard Guide")
    assert created.json()["title"] == "Lanyard Guide"


@pytest.mark.parametrize(
    "name,data",
    [
        ("malware.exe", b"MZ\x90\x00"),
        ("fake.pdf", b"this is not a pdf"),
        ("fake.docx", b"PK\x03\x04 not really a docx"),
        ("binary.txt", b"abc\x00\x01\x02def"),
        ("empty.md", b""),
    ],
)
def test_rejects_bad_files(api, acme_admin, name, data) -> None:
    response = upload(api, acme_admin, name, data)
    assert response.status_code == 422, response.text
    assert api.get("/admin/documents", headers=acme_admin).json()["total"] == 2  # only the seed docs


def test_rejects_oversized_file(api, acme_admin, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 100)
    response = upload(api, acme_admin, "big.txt", b"x" * 101)
    assert response.status_code == 413


def test_duplicate_content_is_rejected(api, acme_admin) -> None:
    assert upload(api, acme_admin, "a.txt", b"Same content here.").status_code == 201
    dup = upload(api, acme_admin, "b.txt", b"Same content here.")
    assert dup.status_code == 409 and "Duplicate" in dup.json()["detail"]


def test_same_content_allowed_in_another_tenant(api, acme_admin, globex_admin) -> None:
    assert upload(api, acme_admin, "a.txt", b"Shared content.").status_code == 201
    assert upload(api, globex_admin, "a.txt", b"Shared content.").status_code == 201


def test_per_tenant_document_cap(api, acme_admin, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_documents_per_tenant", 3)
    assert upload(api, acme_admin, "one.txt", b"one").status_code == 201  # 2 seeded + 1
    full = upload(api, acme_admin, "two.txt", b"two")
    assert full.status_code == 409 and "limit" in full.json()["detail"]


def test_pasted_text_goes_through_the_same_pipeline(api, acme_admin) -> None:
    created = api.post(
        "/admin/documents", headers=acme_admin, json={"title": "Paste", "content": "Hello there."}
    )
    assert created.status_code == 201
    assert detail(api, acme_admin, created.json()["id"])["status"] == "ready"


# ---------- failure handling ----------


def test_pdf_without_text_fails_with_message(api, acme_admin) -> None:
    from pypdf import PdfWriter

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(612, 792)  # what a scanned image-only PDF looks like to a text extractor
    writer.write(buf)
    created = upload(api, acme_admin, "scan.pdf", buf.getvalue())
    d = detail(api, acme_admin, created.json()["id"])
    assert d["status"] == "failed"
    assert "scanned" in d["error"]
    assert d["chunks"] == []


def test_embedding_outage_marks_failed_then_reindex_recovers(api, acme_admin, fake_llm) -> None:
    def down(path, payload):
        raise LLMUnavailable("embedding backend down")

    fake_llm.override = down
    created = upload(api, acme_admin, "later.md", b"# Later\nThe zorblax widget ships in blue.")
    doc_id = created.json()["id"]
    d = detail(api, acme_admin, doc_id)
    assert d["status"] == "failed" and "unavailable" in d["error"]

    fake_llm.override = None
    assert api.post(f"/admin/documents/{doc_id}/reindex", headers=acme_admin).status_code == 200
    d = detail(api, acme_admin, doc_id)
    assert d["status"] == "ready" and d["error"] is None and d["chunk_count"] >= 1


def test_failed_reindex_keeps_previous_chunks_searchable(api, acme_admin, acme_user, fake_llm) -> None:
    created = upload(api, acme_admin, "w.md", b"# Widgets\nThe zorblax widget ships in blue.")
    doc_id = created.json()["id"]
    assert "blue" in ask(api, acme_user, "What colour does the zorblax widget ship in?")["content"]

    fake_llm.override = lambda path, payload: (_ for _ in ()).throw(LLMUnavailable("down"))
    edited = {"content": "# Widgets\nThe zorblax widget ships in red."}  # new text has to be embedded
    api.patch(f"/admin/documents/{doc_id}", headers=acme_admin, json=edited)
    fake_llm.override = None
    d = detail(api, acme_admin, doc_id)
    assert d["status"] == "failed"
    assert len(d["chunks"]) >= 1  # old index untouched
    assert "blue" in ask(api, acme_user, "What colour does the zorblax widget ship in?")["content"]


def test_reindexing_an_unchanged_document_needs_no_embeddings(api, acme_admin, fake_llm) -> None:
    doc_id = upload(api, acme_admin, "w.md", b"# Widgets\nThe zorblax widget ships in blue.").json()["id"]
    embedded = fake_llm.embedded_texts
    fake_llm.override = lambda path, payload: (_ for _ in ()).throw(LLMUnavailable("down"))
    api.post(f"/admin/documents/{doc_id}/reindex", headers=acme_admin)
    fake_llm.override = None
    d = detail(api, acme_admin, doc_id)
    assert d["status"] == "ready" and d["chunk_count"] >= 1  # every passage reused its stored embedding
    assert fake_llm.embedded_texts == embedded


# ---------- list / detail / edit / delete ----------


def test_list_search_filter_and_pagination(api, acme_admin) -> None:
    upload(api, acme_admin, "lanyards.txt", b"Lanyards are blue.")
    upload(api, acme_admin, "scan.pdf", b"%PDF-1.4 garbage that is not parseable")  # becomes failed
    everything = api.get("/admin/documents", headers=acme_admin).json()
    assert everything["total"] == 4

    hit = api.get("/admin/documents?q=lanyard", headers=acme_admin).json()
    assert [d["title"] for d in hit["items"]] == ["lanyards"]
    failed = api.get("/admin/documents?status=failed", headers=acme_admin).json()
    assert [d["title"] for d in failed["items"]] == ["scan"]
    page = api.get("/admin/documents?limit=2&offset=2", headers=acme_admin).json()
    assert len(page["items"]) == 2 and page["total"] == 4
    assert api.get("/admin/documents?status=bogus", headers=acme_admin).status_code == 422


def test_title_edit_updates_citations_without_reindex(api, acme_admin, acme_user) -> None:
    ask(api, acme_user, "What is the return window for returns?")  # warm the cache
    doc = next(
        d
        for d in api.get("/admin/documents", headers=acme_admin).json()["items"]
        if d["title"] == "Acme Return Policy"
    )
    api.patch(f"/admin/documents/{doc['id']}", headers=acme_admin, json={"title": "Returns v2"})
    body = ask(api, acme_user, "What is the return window for returns?")
    assert body["cache_hit"] is False
    assert [c["title"] for c in body["citations"]] == ["Returns v2"]


def test_content_edit_reindexes(api, acme_admin, acme_user) -> None:
    created = api.post(
        "/admin/documents",
        headers=acme_admin,
        json={"title": "Colours", "content": "The zorblax widget ships in blue."},
    ).json()
    api.patch(
        f"/admin/documents/{created['id']}",
        headers=acme_admin,
        json={"content": "The zorblax widget ships in green."},
    )
    d = detail(api, acme_admin, created["id"])
    assert d["status"] == "ready" and "green" in d["content"]
    answer = ask(api, acme_user, "What colour does the zorblax widget ship in?")["content"]
    assert "green" in answer and "blue" not in answer


def test_delete_removes_document_chunks_and_cache(api, acme_admin, acme_user) -> None:
    created = api.post(
        "/admin/documents",
        headers=acme_admin,
        json={"title": "Temp", "content": "The quuxplode gadget is discontinued."},
    ).json()
    assert "discontinued" in ask(api, acme_user, "Is the quuxplode gadget available?")["content"]
    before = chunk_total()
    assert api.delete(f"/admin/documents/{created['id']}", headers=acme_admin).status_code == 204
    assert chunk_total() < before
    assert api.get(f"/admin/documents/{created['id']}", headers=acme_admin).status_code == 404
    assert api.delete(f"/admin/documents/{created['id']}", headers=acme_admin).status_code == 404
    after = ask(api, acme_user, "Is the quuxplode gadget available?")
    assert after["cache_hit"] is False and "discontinued" not in after["content"]


# ---------- isolation & roles ----------


def test_other_tenant_cannot_touch_documents(api, acme_admin, globex_admin) -> None:
    created = upload(api, acme_admin, "secret.md", b"# Secret\nAcme internal pricing.")
    doc_id = created.json()["id"]
    assert doc_id not in [d["id"] for d in api.get("/admin/documents", headers=globex_admin).json()["items"]]
    assert api.get(f"/admin/documents/{doc_id}", headers=globex_admin).status_code == 404
    assert (
        api.patch(f"/admin/documents/{doc_id}", headers=globex_admin, json={"title": "x"}).status_code == 404
    )
    assert api.delete(f"/admin/documents/{doc_id}", headers=globex_admin).status_code == 404
    assert api.post(f"/admin/documents/{doc_id}/reindex", headers=globex_admin).status_code == 404
    assert detail(api, acme_admin, doc_id)["title"] == "secret"  # untouched


def test_customers_and_anonymous_are_blocked_everywhere(api, acme_user) -> None:
    calls = [
        ("get", "/admin/documents"),
        ("post", "/admin/documents"),
        ("post", "/admin/documents/upload"),
        ("get", "/admin/documents/00000000-0000-0000-0000-000000000000"),
        ("post", "/admin/retrieval/test"),
    ]
    for method, path in calls:
        assert getattr(api, method)(path, headers=acme_user).status_code in (403, 422)
        assert getattr(api, method)(path).status_code == 401
    # 403 (not 422) once the body is valid: role is checked before the handler runs
    assert api.post("/admin/retrieval/test", headers=acme_user, json={"query": "x"}).status_code == 403


# ---------- retrieval tester ----------


def test_retrieval_tester_shows_ranks_and_cache_state(api, acme_admin, acme_user) -> None:
    first = api.post(
        "/admin/retrieval/test", headers=acme_admin, json={"query": "express shipping SHIP-EXP"}
    ).json()
    assert first["cache_hit"] is False
    top = first["hits"][0]
    assert top["title"] == "Acme Shipping FAQ"
    assert top["sparse_rank"] == 1 and top["dense_rank"] is not None
    assert all("Globex" not in h["title"] for h in first["hits"])

    ask(api, acme_user, "express shipping SHIP-EXP")  # a real chat query populates the cache
    again = api.post(
        "/admin/retrieval/test", headers=acme_admin, json={"query": "express shipping SHIP-EXP"}
    ).json()
    assert again["cache_hit"] is True
