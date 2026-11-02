"""Test setup. DB-backed tests need Postgres+pgvector; they skip when it is unreachable.

The tests use a dedicated `support_test` database so dev data is never touched.
"""

import os
import sys
from pathlib import Path

# Must be set before app.core.config is first imported/cached.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://support:support@localhost:5432/support_test"
)
os.environ["LLM_API_KEY"] = ""
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
os.environ["SEED_PASSWORD"] = "test-password"
os.environ["BCRYPT_ROUNDS"] = "4"
os.environ["JOBS_MODE"] = "sync"  # background work runs inline so tests can assert on it
os.environ["ENABLE_DEMO_COMPANY_API"] = "true"  # mounts /demo-company for the demo-company tests
os.environ["SEED_DEMO_AGENT"] = "0"  # tests build their own agents; keep the seeded set minimal
os.environ["ALLOW_PRIVATE_CONNECTIONS"] = "false"  # tests must pass with the production SSRF rules

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.main import create_app  # noqa: E402

PASSWORD = "test-password"


def _ensure_test_database() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def _db_available() -> bool:
    try:
        _ensure_test_database()
        from app.db import models  # noqa: F401
        from app.db.base import Base
        from app.db.session import get_engine, init_db

        Base.metadata.drop_all(bind=get_engine())  # no stale schema from earlier runs
        init_db()
        return True
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {exc}")


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """Every test talks to a deterministic fake LLM; nothing here calls the network."""
    from app.core import llm
    from tests.fakes import FakeLLM

    fake = FakeLLM()
    monkeypatch.setattr(llm, "_post", fake)
    return fake


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Company hostnames used in tests resolve to public addresses (no real DNS)."""
    from app.core import ssrf

    table = {"api.acme.test": ["8.8.8.8"], "api.globex.test": ["8.8.4.4"], "hooks.acme.test": ["8.8.8.8"]}
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: table.get(host, []))
    return table


@pytest.fixture
def company():
    """Route action calls to an in-process fake company API (no network)."""
    import httpx

    from app.agents import executor
    from tests.company import FakeCompany

    fake = FakeCompany()
    executor.set_client_override(
        httpx.Client(transport=httpx.MockTransport(fake.handler), follow_redirects=False)
    )
    yield fake
    executor.set_client_override(None)


@pytest.fixture
def receiver():
    """Route outbound webhook deliveries to an in-process receiver (no network)."""
    import httpx

    from app.core import safe_http
    from tests.company import FakeReceiver

    fake = FakeReceiver()
    safe_http.set_client_override(httpx.Client(transport=httpx.MockTransport(fake.handler)))
    yield fake
    safe_http.set_client_override(None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def seeded(_db_available):
    """Wipe all tables and reseed the two tenants for each test."""
    from seed.seed import run_seed

    from app.db.base import Base
    from app.db.session import get_engine, get_session_factory
    from app.rag import cache

    engine = get_engine()
    with engine.begin() as conn:
        # One statement for every table: each separate TRUNCATE costs a disk sync, which dominated
        # test time. CASCADE handles the foreign keys.
        names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE TABLE {names} CASCADE"))
    try:
        cache._redis().flushdb()
    except Exception:
        pass
    with get_session_factory()() as db:
        run_seed(db)
    return True


@pytest.fixture
def api(client, seeded) -> TestClient:
    return client


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def acme_user(api) -> dict[str, str]:
    return login(api, "acme.user@example.com")


@pytest.fixture
def acme_admin(api) -> dict[str, str]:
    return login(api, "acme.admin@example.com")


@pytest.fixture
def globex_user(api) -> dict[str, str]:
    return login(api, "globex.user@example.com")


@pytest.fixture
def globex_admin(api) -> dict[str, str]:
    return login(api, "globex.admin@example.com")
