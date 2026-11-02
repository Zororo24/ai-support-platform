"""background jobs, webhooks and staff approvals

Revision ID: 0009
Revises: 0008
"""

from alembic import op

from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("jobs", "webhook_endpoints", "webhook_deliveries", "approvals"):
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("approvals", "webhook_deliveries", "webhook_endpoints", "jobs"):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
