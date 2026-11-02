import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

BACKEND = Path(__file__).resolve().parents[1]
DB_NAME = "support_migration_data_test"


def alembic(env, *args):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_existing_data_is_migrated_into_a_working_default_agent() -> None:
    """Simulate a database from before agents existed, then upgrade it."""
    try:
        url = make_url(os.environ["DATABASE_URL"])
        admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}"'))
            conn.execute(text(f'CREATE DATABASE "{DB_NAME}"'))
    except Exception as exc:
        pytest.skip(f"Postgres not available: {exc}")

    target = url.set(database=DB_NAME)
    env = {**os.environ, "DATABASE_URL": target.render_as_string(hide_password=False)}
    alembic(env, "upgrade", "head")
    alembic(env, "downgrade", "0005")  # back to the pre-agents schema

    engine = create_engine(target)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), 'old', 'Old Co'), (gen_random_uuid(), 'empty', 'Empty Co')"
            )
        )
        old = conn.scalar(text("SELECT id FROM tenants WHERE slug = 'old'"))
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash, role) "
                "VALUES (gen_random_uuid(), :t, 'a@old.test', 'x', 'admin')"
            ),
            {"t": old},
        )
        for title in ("One", "Two"):
            conn.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, title, source, content) "
                    "VALUES (gen_random_uuid(), :t, :title, 'seed', 'text')"
                ),
                {"t": old, "title": title},
            )

    alembic(env, "upgrade", "head")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT t.slug, c.id AS cid, c.is_default, a.name, a.published_version, "
                "       a.draft_config -> 'knowledge' -> 'collection_ids' ->> 0 AS attached, "
                "       (SELECT count(*) FROM agent_versions v WHERE v.agent_id = a.id AND v.version = 1) AS v1, "
                "       (SELECT count(*) FROM documents d WHERE d.collection_id = c.id) AS docs "
                "FROM tenants t JOIN collections c ON c.tenant_id = t.id JOIN agents a ON a.tenant_id = t.id "
                "ORDER BY t.slug"
            )
        ).all()
        orphans = conn.scalar(text("SELECT count(*) FROM documents WHERE collection_id IS NULL"))
        version = conn.scalar(text("SELECT version_num FROM alembic_version"))
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}"'))
    admin.dispose()

    by_slug = {r.slug: r for r in rows}
    assert set(by_slug) == {"old", "empty"}
    for row in by_slug.values():
        assert row.is_default and row.name == "Support assistant"
        assert row.published_version == 1 and row.v1 == 1
        assert row.attached == str(row.cid)  # the agent answers from the default collection
    assert by_slug["old"].docs == 2 and by_slug["empty"].docs == 0 and orphans == 0
    assert version == "0012"
