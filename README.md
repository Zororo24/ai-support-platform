# AI Support Platform

A multi-tenant platform for building, testing and running AI agents. A company configures an agent in a dashboard (knowledge base, instructions, actions in its own systems, guided workflows, guardrails), publishes a version, and connects it to its own chatbot or website through a small public HTTP API.

There is no end-user widget to embed. The company's backend sends each customer message to `/v1` and renders whatever comes back, so the agent fits into any existing product.

**Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16 + pgvector, Redis, React, Vite, TypeScript.

## Features

| Area | What it does |
|---|---|
| **Agents** | Create from a template; set instructions, language, hand-off behaviour and limits. Draft and immutable **published versions** with one-click rollback and a diff between any two versions |
| **Knowledge base** | Collections of documents (`.md`, `.txt`, `.pdf`, `.docx`, pasted text). Heading-aware chunking that keeps tables intact, hybrid search (vector + keyword, rank fusion), optional query rewriting and reranking, a retrieval tester, and quality reports (recall@k, MRR) |
| **Actions** | Calls into the company's own systems over HTTP, defined in a form: input schema, guard rules (for example "block refunds over $200"), a Test button and encrypted connections. An action can instead be performed by the company's backend (hand-back), so no credentials are stored here |
| **Workflows** | Guided multi-turn processes (collect details, check eligibility, confirm, act) built from templates in a form editor and resumable across messages |
| **Approvals** | Write actions can require the customer's confirmation or a staff member's approval |
| **Webhooks** | HMAC-signed events (hand-offs, approvals, failures, budget alerts) with retries and replay |
| **Testing** | Playground with a live step trace, scenario tests that assert agent behaviour and gate publishing, and replay of past conversations against the draft |
| **Operations** | Run traces, usage per agent, daily token budgets, data retention, PII masking, API key IP allow-lists, rate limits, audit log, optional per-workspace model key |

## How it works

```
 Company's chatbot / website
            │  POST /v1/agents/{id}/messages   (API key)
            ▼
 ┌─────────────────────────── FastAPI ───────────────────────────┐
 │  decision loop ──► reply │ answer_from_knowledge │ call_action │
 │        │                 │ start_workflow        │ handoff     │
 │        ▼                                                       │
 │  policy in code: schema validation, guards, identity binding,  │
 │  confirmations / approvals, token budgets, PII masking         │
 └───────┬───────────────┬───────────────────┬───────────────────┘
         │               │                   │
   Postgres + pgvector   Redis          HTTP executor (SSRF-safe)
   (data, chunks, jobs)  (limits, cache)   → company systems
         │
   background worker: indexing, webhook delivery, expiry, retention
```

The model proposes a JSON decision each step; the platform validates it and enforces policy in code. Customer identity comes from trusted `context.*` values supplied by the company's backend, never from model output.

A response `status` tells the caller what to do next: `completed`, `needs_input`, `needs_confirmation`, `pending_approval`, `action_required`, `handed_off` or `failed`.

## Getting started

Requires Docker, Python 3.12+, Node 20+ and an API key for an OpenAI-compatible model provider (Gemini works with a free key).

```bash
cp .env.example .env            # Windows: copy .env.example .env
```

Edit `.env`:

- `LLM_API_KEY`: your provider key
- `LLM_MODEL`: a current chat model (for example `gemini-3.5-flash-lite`)
- `SECRETS_KEY`: generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `JWT_SECRET`: any long random string

Start the database and Redis, then the backend:

```bash
docker compose up db redis -d

cd backend
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
alembic upgrade head            # creates the schema and the pgvector extension
cd ..
python seed/seed.py             # demo companies, users and a sample agent (embeds documents, needs the key)

cd backend
uvicorn app.main:app --reload --port 8000
```

In a second terminal, start the frontend:

```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173
```

To run everything in containers instead: `docker compose --profile full up --build`, then `python seed/seed.py`.

**Demo logins** (password `demo-password`): `acme.admin@example.com` and `globex.admin@example.com` open the admin dashboard. `acme.user@example.com` and `globex.user@example.com` are customers and only see the original chat and tickets screens.

Log in as an admin, open **Agents → Store assistant** and use the playground to try "Where is my order ACM-1002?". The seeded agent calls a fake store API served by this app (`ENABLE_DEMO_COMPANY_API`, local demos only; it needs `ALLOW_PRIVATE_CONNECTIONS=true`, which disables outbound-address safety checks and must never be set in production).

API docs are served at http://localhost:8000/docs.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `503 model_unavailable` | `LLM_API_KEY` missing or wrong, or the backend was started before `.env` was saved. Restart it. The server log shows the provider's error |
| `ModuleNotFoundError` on install or start | Use Python 3.12+ and re-run `pip install -e ".[dev]"` inside the venv |
| `WinError 10013` when starting uvicorn | Port 8000 is taken. Stop the other process or use `--port 8010` and set `API_PROXY_TARGET=http://localhost:8010` for the frontend |
| `npm` not recognised | Reopen the terminal after installing Node so it picks up the new PATH |
| Seed fails | It embeds documents, so it needs a working model key and Postgres running |

## Configuration

| Setting | Purpose |
|---|---|
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `EMBEDDING_MODEL` | Any OpenAI-compatible provider. `EMBEDDING_DIMENSIONS` must match the stored vector size (1536). Without a working key the API answers `503 model_unavailable` |
| `SECRETS_KEY` | Fernet key that encrypts connection credentials, webhook secrets and workspace model keys. Keep it; losing it makes stored secrets unreadable |
| `JOBS_MODE` | `embedded` (API runs the worker, development), `external` (`python -m app.worker`, production) or `sync` (tests) |
| `TRUST_PROXY_HEADERS` | Read the caller address from `X-Forwarded-For` so API key IP allow-lists work behind a reverse proxy |
| `TENANT_DAILY_TOKEN_BUDGET` | Optional workspace-wide daily token ceiling (0 = off). Per-agent budgets are set in the dashboard |
| `APPROVALS_TTL_HOURS`, `TOOL_RESULT_TTL_MINUTES` | How long staff or the customer's backend have to respond |

After changing the embedding model, re-index with `python seed/seed.py --reindex` or **Knowledge base → Quality → Rebuild index**.

## Public API

```bash
curl -X POST http://localhost:8000/v1/agents/$AGENT_ID/messages \
  -H "Authorization: Bearer $AGENT_KEY" -H "Content-Type: application/json" \
  -d '{"message": "Where is my order ACM-1002?", "session_id": "chat-123",
       "end_user": {"id": "customer-42", "attributes": {"customer_id": "cust_42"}}}'
```

Related endpoints continue a turn: `POST …/confirmations` for customer confirmations and `POST …/tool-results` when the company's backend performs an action. Responses stream over SSE when requested. Errors are `{"error": {"code", "message"}}` with stable codes, and the `/v1` contract is pinned by a snapshot test.

More: [docs/quickstart.md](docs/quickstart.md), [docs/api-reference.md](docs/api-reference.md), Python and Node clients in [clients/](clients/README.md), and a runnable sample website in [examples/chat-site](examples/chat-site).

## Project layout

```
backend/
  app/
    agents/      decision loop, actions, workflows, executor, scenario schema
    api/         /v1 public API and dashboard routes
    core/        settings, security, secrets vault, LLM client, SSRF guard, PII masking
    db/          SQLAlchemy models
    rag/         chunking, hybrid retrieval, rewrite, rerank, evaluation
    services/    agents, documents, jobs, webhooks, approvals, budgets, scenarios
    worker.py    background worker
  alembic/       database migrations
  tests/         test suite
frontend/        React dashboard
clients/         Python and Node clients
examples/        sample chat site
seed/, scripts/  demo data and retrieval evaluation
docs/            quickstart and API reference
```

## Testing

```bash
cd backend
pytest                        # about 600 tests
ruff check . && ruff format --check .
cd ../frontend && npm run build
```

The suite uses a deterministic fake model and fake company APIs, so it needs no provider key or network. Database tests use a separate `support_test` database on the Compose Postgres and Redis db 15 (override with `TEST_DATABASE_URL` and `TEST_REDIS_URL`); they share state and must not run in parallel. CI runs on GitHub Actions ([ci.yml](.github/workflows/ci.yml)).

Covered areas include tenant isolation, SSRF and secret handling, decision-loop policy, workflows, hand-back and approvals, signed webhooks, the job queue, scenario tests and the publish gate, retrieval quality on a golden corpus, budgets, retention, PII masking and the `/v1` contract.

Because the tests use a fake model, they verify the platform's validation, policy and state handling rather than a real model's judgement. To measure retrieval and decisions against a live model, run `python scripts/eval_retrieval.py --record` and then `python scripts/eval_retrieval.py` for regressions, and use **Knowledge base → Quality** and each agent's **Tests** tab in the dashboard.

## Notes

- Background work (indexing, webhook delivery, expiry and retention sweeps) runs on a Postgres job queue. Run at least one worker in production.
- Retention sweeps delete idle conversations of published agents after the agent's `retention_days`; test conversations are removed after three days.
- PII masking is pattern based (emails, Luhn-valid card numbers, phone numbers). It does not catch names or addresses. Use "don't store conversation text" when nothing may be kept.
- Token budgets are soft: a turn already running finishes, and new messages get `429 budget_exceeded` until midnight UTC.
- Query rewriting, reranking, quality checks and generated test questions make extra model calls, so they are opt-in.
- Admins create users with an initial password; there are no email invitations yet. The original customer chat and tickets screens remain from the first version of the project.
