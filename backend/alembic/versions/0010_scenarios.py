"""scenario tests, scenario runs and the publish gate's summary on versions

Revision ID: 0010
Revises: 0009
"""

from alembic import op
from sqlalchemy import text

from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("ALTER TABLE agent_versions ADD COLUMN IF NOT EXISTS test_summary JSONB"))
    for name in ("agent_scenarios", "scenario_runs"):
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("scenario_runs", "agent_scenarios"):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
    bind.execute(text("ALTER TABLE agent_versions DROP COLUMN IF EXISTS test_summary"))
