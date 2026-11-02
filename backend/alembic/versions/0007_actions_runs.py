"""actions platform: connections, runs, run steps, demo-company tables, order demo columns

Revision ID: 0007
Revises: 0006

Idempotent for the same reason as earlier revisions (0001 builds from the current models).
"""

from alembic import op
from sqlalchemy import text

from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("connections", "runs", "run_steps", "demo_tickets", "demo_returns"):
        Base.metadata.tables[name].create(bind, checkfirst=True)
    for statement in (
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_ref VARCHAR(200)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS item_name VARCHAR(200)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ",
    ):
        bind.execute(text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    for statement in (
        "DROP TABLE IF EXISTS demo_returns",
        "DROP TABLE IF EXISTS demo_tickets",
        "DROP TABLE IF EXISTS run_steps",
        "DROP TABLE IF EXISTS runs",
        "DROP TABLE IF EXISTS connections",
        "ALTER TABLE orders DROP COLUMN IF EXISTS delivered_at",
        "ALTER TABLE orders DROP COLUMN IF EXISTS item_name",
        "ALTER TABLE orders DROP COLUMN IF EXISTS customer_ref",
    ):
        bind.execute(text(statement))
