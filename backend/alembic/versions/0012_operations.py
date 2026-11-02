"""API key address allow-lists and per-workspace AI provider keys

Revision ID: 0012
Revises: 0011
"""

from alembic import op
from sqlalchemy import text

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for statement in (
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS allowed_ips JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS llm_api_key_encrypted TEXT",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS llm_key_hint VARCHAR(16)",
    ):
        bind.execute(text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    for statement in (
        "ALTER TABLE tenants DROP COLUMN IF EXISTS llm_key_hint",
        "ALTER TABLE tenants DROP COLUMN IF EXISTS llm_api_key_encrypted",
        "ALTER TABLE api_keys DROP COLUMN IF EXISTS allowed_ips",
    ):
        bind.execute(text(statement))
