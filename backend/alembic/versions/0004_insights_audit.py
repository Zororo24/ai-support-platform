"""message metadata for insights, audit log

Revision ID: 0004
Revises: 0003
"""
from alembic import op
from sqlalchemy import text

from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS meta JSONB"))
    Base.metadata.tables["audit_log"].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP TABLE IF EXISTS audit_log"))
    bind.execute(text("ALTER TABLE messages DROP COLUMN IF EXISTS meta"))
