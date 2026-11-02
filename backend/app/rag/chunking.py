"""Split documents into retrievable pieces.

Each piece keeps its `content` exactly as written (that is what customers are shown and what
answers cite) plus a short `context` such as "Returns policy › Refunds › Timing". The context is
indexed together with the content, so a passage that only says "Two years." is still found by
"how long is the warranty", because its heading path says what it is about.

Markdown tables are never cut mid-row: a table that does not fit is split between rows and the
header row is repeated, so every piece of it is still readable on its own.
"""

import hashlib
import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
CONTEXT_SEPARATOR = " › "


@dataclass(frozen=True)
class Piece:
    content: str
    context: str = ""

    @property
    def embed_text(self) -> str:
        """What is embedded and keyword-indexed: the heading path, then the passage."""
        return f"{self.context}\n{self.content}" if self.context else self.content

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.embed_text.encode("utf-8")).hexdigest()


def chunk_text(text: str, max_chars: int = 1800, overlap: int = 200) -> list[str]:
    """The passages only (no context). Kept for callers that do not need the heading path."""
    return [p.content for p in chunk_document("", text, max_chars=max_chars, overlap=overlap)]


def chunk_document(title: str, text: str, max_chars: int = 1800, overlap: int = 200) -> list[Piece]:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    pieces: list[Piece] = []
    for path, section in _sections(normalized):
        context = _context(title, path)
        for content in _fit(section, max_chars, overlap):
            pieces.append(Piece(content=content, context=context))
    return pieces


def _context(title: str, path: list[str]) -> str:
    """ "Title › Section › Subsection", without repeating a top heading that is the title itself."""
    parts: list[str] = []
    for part in [title.strip(), *path]:
        if part and (not parts or parts[-1].lower() != part.lower()):
            parts.append(part)
    return CONTEXT_SEPARATOR.join(parts)


# ---- sections -------------------------------------------------------------------------------------


def _sections(text: str) -> list[tuple[list[str], str]]:
    """(heading path, section text including its heading line), split at every heading.

    A heading with nothing under it (a document title followed straight away by a subheading) is not
    a passage of its own; it stays with the section that follows.
    """
    sections: list[tuple[list[str], str]] = []
    stack: list[tuple[int, str]] = []
    path: list[str] = []
    current: list[str] = []
    carried: list[str] = []

    def flush() -> None:
        body = "\n".join(current).strip()
        if not body:
            return
        if all(HEADING_RE.match(line) for line in body.split("\n") if line.strip()):
            carried.append(body)
            return
        sections.append((list(path), "\n\n".join([*carried, body])))
        carried.clear()

    for line in text.split("\n"):
        match = HEADING_RE.match(line)
        if match and current:
            flush()
            current = []
        if match:
            level, heading = len(match.group(1)), match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            path = [h for _, h in stack]
        current.append(line)
    flush()
    if carried:  # the document ends on a bare heading
        sections.append((list(path), "\n\n".join(carried)))
    return sections


# ---- fitting a section into pieces ---------------------------------------------------------------------


def _fit(section: str, max_chars: int, overlap: int) -> list[str]:
    if len(section) <= max_chars:
        return [section]
    pieces: list[str] = []
    buffer = ""
    for block in _blocks(section):
        if len(block) > max_chars:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(_split_big(block, max_chars, overlap))
            continue
        joined = f"{buffer}\n\n{block}" if buffer else block
        if len(joined) <= max_chars:
            buffer = joined
        else:
            pieces.append(buffer)
            buffer = block
    if buffer:
        pieces.append(buffer)
    return [p.strip() for p in pieces if p.strip()]


def _blocks(section: str) -> list[str]:
    """Paragraphs and whole tables, in order."""
    blocks: list[str] = []
    paragraph: list[str] = []
    table: list[str] = []

    def end_paragraph() -> None:
        if paragraph:
            blocks.append("\n".join(paragraph))
            paragraph.clear()

    def end_table() -> None:
        if table:
            blocks.append("\n".join(table))
            table.clear()

    for line in section.split("\n"):
        if _is_table_row(line):
            end_paragraph()
            table.append(line)
        elif not line.strip():
            end_paragraph()
            end_table()
        else:
            end_table()
            paragraph.append(line)
    end_paragraph()
    end_table()
    return blocks


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _split_big(block: str, max_chars: int, overlap: int) -> list[str]:
    lines = block.split("\n")
    if all(_is_table_row(line) for line in lines):
        return _split_table(lines, max_chars)
    return _windows(block, max_chars, overlap)


def _split_table(rows: list[str], max_chars: int) -> list[str]:
    """Group rows so each piece fits, repeating the header (and its separator row)."""
    header = rows[:2] if len(rows) > 1 and re.fullmatch(r"[\s|:\-]+", rows[1]) else rows[:1]
    body = rows[len(header) :]
    head = "\n".join(header)
    pieces: list[str] = []
    current: list[str] = []
    size = len(head)
    for row in body:
        if current and size + 1 + len(row) > max_chars:
            pieces.append("\n".join([head, *current]))
            current, size = [], len(head)
        current.append(row)
        size += 1 + len(row)
    if current or not pieces:
        pieces.append("\n".join([head, *current]))
    # A single row longer than the limit cannot be split without breaking it; keep it whole.
    return pieces


def _windows(text: str, max_chars: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        pieces.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(end - overlap, start + 1)
    return pieces
