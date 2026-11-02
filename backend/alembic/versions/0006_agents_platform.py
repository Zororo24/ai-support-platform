"""agent platform foundation: collections, agents, versions, API keys, sessions

Revision ID: 0006
Revises: 0005

Idempotent (0001 builds the schema from the current models, so on a fresh database the
tables already exist). On an existing database this also migrates the data: every tenant
gets a default collection holding all its documents and a published "Support assistant"
agent that answers from it, so nothing stops working.
"""

from alembic import op
from sqlalchemy import text

from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("collections", "agents", "agent_versions", "api_keys", "sessions"):
        Base.metadata.tables[name].create(bind, checkfirst=True)
    for statement in (
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS collection_id UUID REFERENCES collections(id)",
        "CREATE INDEX IF NOT EXISTS ix_documents_collection_id ON documents (collection_id)",
        "ALTER TABLE conversations ALTER COLUMN user_id DROP NOT NULL",
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS agent_id UUID"
        " REFERENCES agents(id) ON DELETE CASCADE",
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS session_id UUID"
        " REFERENCES sessions(id) ON DELETE CASCADE",
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS end_user_ref VARCHAR(200)",
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'dashboard'",
        "CREATE INDEX IF NOT EXISTS ix_conversations_agent_id ON conversations (agent_id)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_session_id ON conversations (session_id)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_end_user_ref ON conversations (end_user_ref)",
    ):
        bind.execute(text(statement))

    # Data migration, only for tenants that have no agent yet.
    bind.execute(
        text(
            """
            INSERT INTO collections (id, tenant_id, name, description, is_default)
            SELECT gen_random_uuid(), t.id, 'General', 'Default knowledge', true
            FROM tenants t
            WHERE NOT EXISTS (
                SELECT 1 FROM collections c WHERE c.tenant_id = t.id AND c.is_default
            )
            """
        )
    )
    bind.execute(
        text(
            """
            UPDATE documents d SET collection_id = c.id
            FROM collections c
            WHERE d.collection_id IS NULL AND c.tenant_id = d.tenant_id AND c.is_default
            """
        )
    )
    bind.execute(
        text(
            """
            INSERT INTO agents (id, tenant_id, name, slug, description, draft_config, published_version)
            SELECT gen_random_uuid(), t.id, 'Support assistant', 'support-assistant',
                   'Answers customer questions from your documents.',
                   jsonb_build_object(
                       'instructions', 'Be concise, friendly and accurate.',
                       'knowledge', jsonb_build_object('collection_ids', jsonb_build_array(c.id::text))
                   ),
                   1
            FROM tenants t
            JOIN collections c ON c.tenant_id = t.id AND c.is_default
            WHERE NOT EXISTS (SELECT 1 FROM agents a WHERE a.tenant_id = t.id)
            """
        )
    )
    bind.execute(
        text(
            """
            INSERT INTO agent_versions (id, tenant_id, agent_id, version, config, note)
            SELECT gen_random_uuid(), a.tenant_id, a.id, 1, a.draft_config, 'Migrated from the original assistant'
            FROM agents a
            WHERE a.published_version = 1
              AND NOT EXISTS (SELECT 1 FROM agent_versions v WHERE v.agent_id = a.id)
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    for statement in (
        "ALTER TABLE conversations DROP COLUMN IF EXISTS source",
        "ALTER TABLE conversations DROP COLUMN IF EXISTS end_user_ref",
        "ALTER TABLE conversations DROP COLUMN IF EXISTS session_id",
        "ALTER TABLE conversations DROP COLUMN IF EXISTS agent_id",
        "ALTER TABLE documents DROP COLUMN IF EXISTS collection_id",
        "DROP TABLE IF EXISTS api_keys",
        "DROP TABLE IF EXISTS sessions",
        "DROP TABLE IF EXISTS agent_versions",
        "DROP TABLE IF EXISTS agents",
        "DROP TABLE IF EXISTS collections",
    ):
        bind.execute(text(statement))
