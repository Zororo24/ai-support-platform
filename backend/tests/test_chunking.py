from app.rag.chunking import chunk_document, chunk_text


def test_splits_on_headings() -> None:
    chunks = chunk_text("# A\none\n\n## B\ntwo")
    assert chunks == ["# A\none", "## B\ntwo"]


def test_long_section_is_windowed_with_overlap() -> None:
    chunks = chunk_text("x" * 5000, max_chars=1000, overlap=100)
    assert len(chunks) > 4
    assert all(len(c) <= 1000 for c in chunks)


def test_empty_text() -> None:
    assert chunk_text("   \n ") == []
    assert chunk_document("Title", "   ") == []


def test_each_passage_knows_the_headings_above_it() -> None:
    text = "# Guide\nIntro.\n\n## Setup\nInstall it.\n\n### Linux\nUse apt.\n\n## Usage\nRun it."
    pieces = chunk_document("Manual", text)
    assert [p.context for p in pieces] == [
        "Manual › Guide",
        "Manual › Guide › Setup",
        "Manual › Guide › Setup › Linux",
        "Manual › Guide › Usage",
    ]
    assert [p.content.split("\n")[0] for p in pieces] == ["# Guide", "## Setup", "### Linux", "## Usage"]


def test_a_title_is_not_repeated_in_the_context() -> None:
    (piece,) = chunk_document("Warranty", "# warranty\nTwo years.")
    assert piece.context == "Warranty"


def test_a_bare_title_stays_with_the_section_under_it() -> None:
    pieces = chunk_document("Warranty", "# Warranty\n\n## Duration\nTwo years from the purchase date.")
    assert len(pieces) == 1  # no passage that is only a heading
    assert pieces[0].content == "# Warranty\n\n## Duration\nTwo years from the purchase date."
    assert pieces[0].context == "Warranty › Duration"
    assert pieces[0].embed_text.startswith("Warranty › Duration\n")


def test_text_without_headings_has_only_the_title_as_context() -> None:
    pieces = chunk_document("Notes", "Plain text.\n\nMore text.")
    assert [p.context for p in pieces] == ["Notes"]


def test_a_passage_hash_changes_with_its_text_and_its_heading_path() -> None:
    a = chunk_document("Doc", "## One\nsame words")[0]
    assert a.digest == chunk_document("Doc", "## One\nsame words")[0].digest
    assert a.digest != chunk_document("Doc", "## One\nother words")[0].digest
    assert a.digest != chunk_document("Renamed", "## One\nsame words")[0].digest


TABLE = "| Method | Cost |\n| --- | --- |\n" + "\n".join(f"| Option {i} | ${i}.00 |" for i in range(40))


def test_a_table_that_fits_is_never_cut() -> None:
    text = "## Prices\nIntro text.\n\n" + TABLE + "\n\nClosing note."
    pieces = chunk_document("Doc", text, max_chars=3000)
    assert len(pieces) == 1 and TABLE in pieces[0].content


def test_a_long_table_splits_between_rows_and_repeats_its_header() -> None:
    pieces = chunk_document("Doc", "## Prices\n\n" + TABLE, max_chars=300)
    tables = [p.content for p in pieces if "| Option" in p.content]
    assert len(tables) > 1
    rows = []
    for piece in tables:
        lines = [line for line in piece.split("\n") if line.startswith("|")]
        assert lines[0] == "| Method | Cost |" and lines[1] == "| --- | --- |"  # readable on its own
        assert all(line.endswith("|") for line in lines)  # no row cut in half
        rows += lines[2:]
    assert rows == TABLE.split("\n")[2:]  # every row exactly once, in order


def test_paragraphs_are_packed_without_splitting_them() -> None:
    paragraphs = [f"Paragraph {i}. " + "word " * 40 for i in range(10)]
    pieces = chunk_document("Doc", "\n\n".join(paragraphs), max_chars=500)
    assert all(len(p.content) <= 500 for p in pieces)
    assert len(pieces) > 2
    joined = "\n\n".join(p.content for p in pieces)
    assert all(p.strip() in joined for p in paragraphs)  # none was cut
