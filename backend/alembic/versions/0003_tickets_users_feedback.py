"""tickets workflow, ticket comments, user activation, message payload + feedback

Revision ID: 0003
Revises: 0002

Idempotent for the same reason as 0002 (0001 builds from the current models).
"""
from alembic import op
from sqlalchemy import text

from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            """
            DO $$ BEGIN
                CREATE TYPE ticket_priority AS ENUM ('low', 'normal', 'high');
            EXCEPTION WHEN duplicate_object THEN NULL; END $$;
            """
        )
    )
    for statement in (
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS priority ticket_priority NOT NULL DEFAULT 'normal'",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS assignee_id UUID REFERENCES users(id)",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS conversation_id UUID"
        " REFERENCES conversations(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS ix_tickets_assignee_id ON tickets (assignee_id)",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS payload JSONB",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS feedback SMALLINT",
    ):
        bind.execute(text(statement))
    Base.metadata.tables["ticket_comments"].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP TABLE IF EXISTS ticket_comments"))
    for table, column in (
        ("messages", "feedback"),
        ("messages", "payload"),
        ("tickets", "conversation_id"),
        ("tickets", "assignee_id"),
        ("tickets", "priority"),
        ("users", "is_active"),
    ):
        bind.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
    bind.execute(text("DROP TYPE IF EXISTS ticket_priority"))
