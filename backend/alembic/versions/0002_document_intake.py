"""document intake: status, error, file metadata, content hash, chunk count, stored upload

Revision ID: 0002
Revises: 0001

Idempotent on purpose: 0001 builds the schema from the *current* models, so on a fresh database
these columns already exist by the time this revision runs. On databases created before this
revision they are added here (existing documents are marked `ready`).
"""
from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            """
            DO $$ BEGIN
                CREATE TYPE document_status AS ENUM ('pending', 'processing', 'ready', 'failed');
            EXCEPTION WHEN duplicate_object THEN NULL; END $$;
            """
        )
    )
    for statement in (
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS status document_status NOT NULL DEFAULT 'ready'",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS error VARCHAR(500)",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_name VARCHAR(300)",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS mime_type VARCHAR(120)",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS size_bytes INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id)",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_data BYTEA",
        "CREATE INDEX IF NOT EXISTS ix_documents_content_hash ON documents (content_hash)",
        # Backfill counts for documents that were indexed before chunk_count existed.
        "UPDATE documents d SET chunk_count = (SELECT count(*) FROM chunks c WHERE c.document_id = d.id)"
        " WHERE d.chunk_count = 0",
    ):
        bind.execute(text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    for column in (
        "status", "error", "file_name", "mime_type", "size_bytes",
        "content_hash", "chunk_count", "created_by", "file_data",
    ):
        bind.execute(text(f"ALTER TABLE documents DROP COLUMN IF EXISTS {column}"))
    bind.execute(text("DROP TYPE IF EXISTS document_status"))
