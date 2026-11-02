"""users.token_version for logout-everywhere / token revocation

Revision ID: 0005
Revises: 0004
"""
from alembic import op
from sqlalchemy import text

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0")
    )


def downgrade() -> None:
    op.get_bind().execute(text("ALTER TABLE users DROP COLUMN IF EXISTS token_version"))
