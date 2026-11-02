"""run summaries for the runs list: outcome and short previews

Revision ID: 0008
Revises: 0007
"""

from alembic import op
from sqlalchemy import text

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for statement in (
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS outcome VARCHAR(32)",
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS input_preview VARCHAR(300)",
        "ALTER TABLE runs ADD COLUMN IF NOT EXISTS reply_preview VARCHAR(300)",
        "CREATE INDEX IF NOT EXISTS ix_runs_tenant_started ON runs (tenant_id, started_at)",
    ):
        bind.execute(text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP INDEX IF EXISTS ix_runs_tenant_started"))
    for column in ("reply_preview", "input_preview", "outcome"):
        bind.execute(text(f"ALTER TABLE runs DROP COLUMN IF EXISTS {column}"))
