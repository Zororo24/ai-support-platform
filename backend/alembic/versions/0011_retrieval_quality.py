"""contextual chunks, passage hashes (incremental re-index), retrieval probes and quality reports

Revision ID: 0011
Revises: 0010
"""

from alembic import op
from sqlalchemy import text

from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return bool(
        bind.scalar(
            text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
            {"t": table, "c": column},
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "chunks", "context"):
        # The keyword index now covers the heading path too, so the generated column is rebuilt.
        bind.execute(text("DROP INDEX IF EXISTS ix_chunks_tsv"))
        bind.execute(text("ALTER TABLE chunks DROP COLUMN IF EXISTS tsv"))
        bind.execute(text("ALTER TABLE chunks ADD COLUMN context TEXT NOT NULL DEFAULT ''"))
        bind.execute(text("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)"))
        bind.execute(
            text(
                "ALTER TABLE chunks ADD COLUMN tsv tsvector "
                "GENERATED ALWAYS AS (to_tsvector('english', context || ' ' || content)) STORED"
            )
        )
        bind.execute(text("CREATE INDEX IF NOT EXISTS ix_chunks_tsv ON chunks USING gin (tsv)"))
        # With an empty context the embedded text is the content itself, so its hash is known.
        bind.execute(
            text(
                "UPDATE chunks SET content_hash = encode(sha256(convert_to(content, 'UTF8')), 'hex') "
                "WHERE content_hash IS NULL"
            )
        )
    for name in ("retrieval_probes", "quality_reports"):
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("quality_reports", "retrieval_probes"):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
    bind.execute(text("DROP INDEX IF EXISTS ix_chunks_tsv"))
    bind.execute(text("ALTER TABLE chunks DROP COLUMN IF EXISTS tsv"))
    bind.execute(text("ALTER TABLE chunks DROP COLUMN IF EXISTS content_hash"))
    bind.execute(text("ALTER TABLE chunks DROP COLUMN IF EXISTS context"))
    bind.execute(
        text("ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED")
    )
    bind.execute(text("CREATE INDEX IF NOT EXISTS ix_chunks_tsv ON chunks USING gin (tsv)"))
