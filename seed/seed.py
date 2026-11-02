"""Idempotent dev seed: two tenants, four users, fake orders, knowledge docs.

Run from the repo root (needs the backend dependencies and a migrated/initialised DB):
    python seed/seed.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.models import Document, Order, Tenant, User, UserRole  # noqa: E402

KNOWLEDGE_DIR = ROOT / "seed" / "knowledge"

# (order number, status, total in cents, item, owning customer, days since delivery or None)
TENANTS = {
    "acme": {
        "name": "Acme Corp",
        "orders": [
            ("ACM-1001", "shipped", 4999, "Wireless mouse", "cust_acme_1", None),
            ("ACM-1002", "delivered", 12900, "Noise-cancelling headphones", "cust_acme_1", 12),
            ("ACM-1003", "delivered", 3500, "Phone case", "cust_acme_1", 45),
            ("ACM-2001", "delivered", 8900, "Desk lamp", "cust_acme_2", 5),
        ],
    },
    "globex": {
        "name": "Globex Inc",
        "orders": [("GLX-2001", "processing", 12900, "Standing desk", "cust_globex_1", None)],
    },
}


def seed_password() -> str:
    return os.environ.get("SEED_PASSWORD", "demo-password")


def seed_core(db: Session) -> dict[str, Tenant]:
    tenants: dict[str, Tenant] = {}
    for slug, spec in TENANTS.items():
        tenant = db.scalars(select(Tenant).where(Tenant.slug == slug)).first()
        if tenant is None:
            tenant = Tenant(slug=slug, name=spec["name"])
            db.add(tenant)
            db.flush()
        tenants[slug] = tenant

        for role in (UserRole.customer, UserRole.admin):
            label = "user" if role == UserRole.customer else "admin"
            email = f"{slug}.{label}@example.com"
            exists = db.scalars(
                select(User).where(User.tenant_id == tenant.id, User.email == email)
            ).first()
            if exists is None:
                db.add(
                    User(
                        tenant_id=tenant.id,
                        email=email,
                        password_hash=hash_password(seed_password()),
                        role=role,
                    )
                )

        for number, status, total, item, customer, delivered_days_ago in spec["orders"]:
            delivered_at = (
                datetime.now(timezone.utc) - timedelta(days=delivered_days_ago)
                if delivered_days_ago is not None
                else None
            )
            order = db.scalars(
                select(Order).where(Order.tenant_id == tenant.id, Order.order_number == number)
            ).first()
            if order is None:
                db.add(
                    Order(
                        tenant_id=tenant.id,
                        order_number=number,
                        status=status,
                        total_cents=total,
                        item_name=item,
                        customer_ref=customer,
                        delivered_at=delivered_at,
                    )
                )
            elif order.customer_ref is None:  # databases seeded before these columns existed
                order.item_name, order.customer_ref, order.delivered_at = item, customer, delivered_at
    db.commit()
    return tenants


def seed_documents(db: Session, tenants: dict[str, Tenant]) -> None:
    from app.services.documents import ingest_document

    for slug, tenant in tenants.items():
        admin = db.scalars(
            select(User).where(User.tenant_id == tenant.id, User.role == UserRole.admin)
        ).first()
        for path in sorted((KNOWLEDGE_DIR / slug).glob("*.md")):
            text = path.read_text(encoding="utf-8")
            title = text.splitlines()[0].lstrip("# ").strip() or path.stem
            exists = db.scalars(
                select(Document).where(Document.tenant_id == tenant.id, Document.title == title)
            ).first()
            if exists is None:
                ingest_document(db, user=admin, title=title, content=text, source="seed")


def seed_agents(db: Session, tenants: dict[str, Tenant]) -> None:
    from app.services.agents import ensure_default_agent

    for tenant in tenants.values():
        admin = db.scalars(
            select(User).where(User.tenant_id == tenant.id, User.role == UserRole.admin)
        ).first()
        ensure_default_agent(db, tenant_id=tenant.id, user=admin)


def demo_actions(connection_id: str) -> list[dict]:
    """The three actions of the seeded Store assistant, all served by the demo-company API."""
    order_pattern = "^[A-Za-z]{3}-[0-9]{3,}$"
    return [
        {
            "name": "get_order",
            "description": "Look up one of the customer's orders by order number (status, item, delivery date).",  # noqa: E501
            "connection_id": connection_id,
            "request": {
                "method": "GET",
                "path": "/orders/{{args.order_number}}",
                "query": {"customer_id": "{{context.customer_id}}"},
            },
            "parameters": {
                "order_number": {
                    "type": "string",
                    "required": True,
                    "pattern": order_pattern,
                    "description": "Order number such as ACM-1002",
                },
            },
            "effect": "read",
            "response": {
                "expose": [
                    "$.order_number",
                    "$.status",
                    "$.item_name",
                    "$.total_cents",
                    "$.delivered_at",
                    "$.days_since_delivery",
                ]
            },
            "mock": {
                "order_number": "ACM-1002",
                "status": "delivered",
                "item_name": "Headphones",
                "total_cents": 12900,
                "days_since_delivery": 12,
            },
        },
        {
            "name": "create_ticket",
            "description": "Open a support ticket so a person follows up with the customer.",
            "connection_id": connection_id,
            "request": {
                "method": "POST",
                "path": "/tickets",
                "body": {
                    "subject": "{{args.subject}}",
                    "body": "{{args.details}}",
                    "customer_id": "{{context.customer_id}}",
                },
            },
            "parameters": {
                "subject": {
                    "type": "string",
                    "required": True,
                    "max_length": 200,
                    "description": "Short title of the problem",
                },
                "details": {
                    "type": "string",
                    "required": True,
                    "max_length": 2000,
                    "description": "What the customer needs, in a few sentences",
                },
            },
            "effect": "write",
            "confirm_summary": "Open a support ticket: {{args.subject}}",
            "idempotency_header": "Idempotency-Key",
            "response": {"expose": ["$.ticket_id", "$.status"]},
            "mock": {"ticket_id": "TCK-MOCK", "status": "open"},
        },
        {
            "name": "create_return",
            "description": "Start a return for a delivered order. Returns are accepted within 30 days of delivery.",  # noqa: E501
            "connection_id": connection_id,
            "request": {
                "method": "POST",
                "path": "/returns",
                "body": {
                    "order_number": "{{args.order_number}}",
                    "reason": "{{args.reason}}",
                    "customer_id": "{{context.customer_id}}",
                },
            },
            "parameters": {
                "order_number": {"type": "string", "required": True, "pattern": order_pattern},
                "reason": {
                    "type": "string",
                    "required": True,
                    "max_length": 500,
                    "description": "Why the customer is returning it",
                },
            },
            "effect": "write",
            "confirm_summary": "Start a return for order {{args.order_number}} (reason: {{args.reason}})",
            "idempotency_header": "Idempotency-Key",
            "response": {"expose": ["$.return_id", "$.label_url", "$.status"]},
            "mock": {
                "return_id": "RET-MOCK",
                "label_url": "https://returns.demo.test/labels/mock",
                "status": "created",
            },
        },
    ]


def seed_demo_agent(db: Session, tenants: dict[str, Tenant]) -> int:
    """Add a published "Store assistant" (knowledge + order/ticket/return actions) per tenant.

    It calls the demo-company API over HTTP through a normal Connection. That needs
    ENABLE_DEMO_COMPANY_API=true and ALLOW_PRIVATE_CONNECTIONS=true (development only).
    Returns how many agents were created.
    """
    from app.api.routes.demo_company import demo_key
    from app.db.models import Agent
    from app.services import agents as agents_service
    from app.services import collections as collections_service
    from app.services import connections as connections_service

    base_url = os.environ.get("DEMO_COMPANY_URL", "http://localhost:8000/demo-company")
    created = 0
    for slug, tenant in tenants.items():
        existing = db.scalars(
            select(Agent).where(Agent.tenant_id == tenant.id, Agent.name == "Store assistant")
        ).first()
        if existing:
            continue
        admin = db.scalars(
            select(User).where(User.tenant_id == tenant.id, User.role == UserRole.admin)
        ).first()
        try:
            connection = connections_service.create_connection(
                db,
                tenant_id=tenant.id,
                user_id=admin.id if admin else None,
                name="Demo store",
                base_url=base_url,
                auth_type="bearer",
                auth_header_name=None,
                secret=demo_key(slug),
            )
        except connections_service.ConnectionRuleError as exc:
            print(f"Skipping the Store assistant for {slug}: {exc}")
            print("  (for local demos set ALLOW_PRIVATE_CONNECTIONS=true and ENABLE_DEMO_COMPANY_API=true)")
            return created
        default = collections_service.default_collection(db, tenant_id=tenant.id)
        agent = agents_service.create_agent(
            db,
            user=admin,
            tenant_id=tenant.id,
            name="Store assistant",
            description="Answers questions, checks orders, opens tickets and starts returns.",
            config={
                "instructions": (
                    "You are the store's friendly support assistant. Keep answers short. "
                    "To check an order, ask for the order number. Returns are accepted within 30 days "
                    "of delivery: check the order's delivery date first, ask the reason, then start "
                    "the return."
                ),
                "knowledge": {"collection_ids": [str(default.id)]},
                "actions": demo_actions(str(connection.id)),
            },
        )
        agents_service.publish(db, agent=agent, user=admin, note="Seeded demo agent")
        created += 1
    return created


def run_seed(db: Session) -> dict[str, Tenant]:
    tenants = seed_core(db)
    seed_documents(db, tenants)
    seed_agents(db, tenants)
    if os.environ.get("SEED_DEMO_AGENT", "1") != "0":
        seed_demo_agent(db, tenants)
    return tenants


def reindex_all(db: Session) -> int:
    """Rebuild every document's chunks/embeddings (needed after changing embedding model/key)."""
    from app.services.documents import process_in_session

    docs = list(db.scalars(select(Document)))
    for doc in docs:
        process_in_session(db, doc.id)
    return len(docs)


def main() -> None:
    from app.core.llm import LLMUnavailable
    from app.db.session import get_session_factory, init_db

    init_db()
    try:
        with get_session_factory()() as db:
            if "--reindex" in sys.argv:
                print(f"Re-embedded {reindex_all(db)} documents")
            else:
                run_seed(db)
                print("Seeded tenants: acme, globex")
    except LLMUnavailable as exc:
        sys.exit(f"Seeding documents needs working embeddings ({exc}). Set LLM_API_KEY in .env and retry.")
    print(f"Users: <tenant>.user@example.com / <tenant>.admin@example.com, password '{seed_password()}'")


if __name__ == "__main__":
    main()
