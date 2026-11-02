# ai-support-platform

A multi-tenant **AI agent platform**. A company configures an agent in the dashboard (knowledge, instructions, actions in its own systems, guided multi-step processes, guardrails), publishes a version, and plugs it into its **own** chatbot or website through a small HTTP API. There is no end-user screen to embed: the company's backend sends each customer message to `/v1`, and shows what comes back.

**Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, PostgreSQL 16 + pgvector, Redis, React + Vite + TypeScript. LangGraph only powers the legacy dashboard chat.

Design, decisions and phase history: [PLAN.md](./PLAN.md). Docs for integrators: [docs/quickstart.md](docs/quickstart.md) and [docs/api-reference.md](docs/api-reference.md). Python and Node clients: [clients/](clients/README.md). A runnable sample website: [examples/chat-site](examples/chat-site).

> **Verification status (read this).** Every test here runs against a deterministic fake model and fake company APIs; nothing in this repository has been run against a real LLM provider yet. The tests prove what the platform does with whatever the model returns (validation, policy, state, retries). They cannot prove that a real model decides well, that real embeddings retrieve well, or that your prompts suit your model. Before relying on it, follow [Checking with a real model](#checking-with-a-real-model).

## What it does

| Area | What a company gets |
|---|---|
| **Agents** | Create from a template; instructions, language, handoff behaviour, limits. Draft and **published versions** (immutable; roll back in one click; diff between any two) |
| **Knowledge** | Collections of documents (`.md/.txt/.pdf/.docx`/pasted text), heading-aware chunking (tables kept intact), hybrid search (meaning + keywords), optional query rewriting and reranking, a **retrieval tester**, and a **quality report** that measures whether search finds the right document |
| **Actions** | Things the agent may do in the company's systems over HTTP: form-based definition, input schema, safety rules (e.g. block refunds over $200), a **Test** button, encrypted **Connections**. Or let the company's own backend perform them (**hand-back**, no credentials held here) |
| **Processes** | Guided multi-turn workflows (collect details, check eligibility, confirm, act) built from templates in a form editor; resumable across messages |
| **Approvals** | Writes can need the customer's confirmation, or a person on the company's team (**Approvals** screen) |
| **Webhooks** | Signed, retried, replayable events: hand-offs, approval requests/decisions, failures, budget alerts |
| **Testing** | Playground with live trace and sample responses; **scenario tests** that assert behaviour and run before every publish; **replay** past conversations against the draft |
| **Operations** | Runs and step traces, usage per agent, **daily token budgets**, **retention** and **PII masking**, API key address allow-lists, rate limits, audit log, optional per-workspace AI provider key |

## Running locally

Prerequisites: Docker, Python 3.12+, Node 20+, and an LLM API key (see below).

```powershell
copy .env.example .env        # then set LLM_API_KEY (and SECRETS_KEY, see below)
docker compose up db redis -d

cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
alembic upgrade head          # schema + pgvector extension
cd ..
python seed/seed.py           # two companies, users, a demo agent (needs the LLM key: it embeds the documents)

cd backend
uvicorn app.main:app --reload --port 8000     # also runs the background worker (JOBS_MODE=embedded)
```

In another terminal:

```powershell
cd frontend
npm install
npm run dev                   # http://localhost:5173
```

Everything in Docker: `docker compose --profile full up --build`, then `python seed/seed.py`. That starts the API, a separate **worker** (`JOBS_MODE=external`) and the web app.

Demo logins (password `demo-password`, or `SEED_PASSWORD`): `acme.admin@example.com`, `globex.admin@example.com` (and `*.user@example.com`, which only use the legacy chat). The seeded **Store assistant** agent calls a fake store API served by this app (`ENABLE_DEMO_COMPANY_API`, local demos only; it requires `ALLOW_PRIVATE_CONNECTIONS=true`, which turns off the outbound-address safety checks and must never be set in production).

### Configuration worth knowing

| Setting | Purpose |
|---|---|
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `EMBEDDING_MODEL` | Any OpenAI-compatible provider. Defaults target Gemini. `EMBEDDING_DIMENSIONS` must equal the column size (1536). Without a working key the API answers `503 model_unavailable`; there is no rule-based fallback |
| `SECRETS_KEY` | Encrypts connection credentials, webhook secrets and per-workspace AI keys (Fernet). Set it before storing any; losing it makes stored secrets unreadable |
| `JOBS_MODE` | `embedded` (API runs the worker, dev), `external` (`python -m app.worker`, production), `sync` (tests) |
| `TRUST_PROXY_HEADERS` | Read the caller address from `X-Forwarded-For` (last hop) so API key address allow-lists work behind a reverse proxy |
| `TENANT_DAILY_TOKEN_BUDGET` | Optional workspace-wide daily token ceiling (0 = off); per-agent budgets are set in the dashboard |
| `APPROVALS_TTL_HOURS`, `TOOL_RESULT_TTL_MINUTES` | How long staff / the customer's backend have to answer |

If you change the embedding model, re-index (`python seed/seed.py --reindex`, or **Knowledge base → Quality → Rebuild index**) so stored vectors match.

## The public API in one minute

```bash
curl -X POST $BASE/v1/agents/$AGENT_ID/messages \
  -H "Authorization: Bearer $AGENT_KEY" -H "Content-Type: application/json" \
  -d '{"message": "Where is my order ACM-1002?", "session_id": "chat-123",
       "end_user": {"id": "customer-42", "attributes": {"customer_id": "cust_42"}}}'
```

The response `status` says what your code does next: `completed`, `needs_input`, `needs_confirmation` (→ `POST …/confirmations`), `pending_approval` (→ wait for a webhook), `action_required` (→ do it, then `POST …/tool-results`), `handed_off`, `failed`. Errors are `{"error": {"code", "message"}}` with stable codes. Full reference: [docs/api-reference.md](docs/api-reference.md). The contract is pinned by a snapshot test (`tests/snapshots/openapi_v1.json`) and `/v1` only evolves additively.

Dashboard API docs: http://localhost:8000/docs.

## Checking with a real model

Two scripts need a real key and a running database:

```powershell
python scripts/eval_retrieval.py --record     # first time: records recall@1/3/5 and MRR for the golden questions
python scripts/eval_retrieval.py              # afterwards: exits 1 if retrieval got worse
python scripts/eval_retrieval.py --rerank
```

and, in the dashboard, **Knowledge base → Quality** (AI-written test questions for *your* documents) and the agent's **Tests** tab (scenarios, which exercise the real model's decisions when you run them). `scripts/run_golden.py` exercises the legacy chat against a running server.

Not yet verified against a real model: JSON-decision reliability, rerank and query-rewrite prompts, the quality of generated test questions, and the numbers in `tests/golden/baseline_fake.json` (which come from the fake embedder and only guard the chunking/fusion logic).

## Tests, lint, CI

```powershell
cd backend
pytest                        # ~600 tests (599 at last run); deterministic fake LLM, no key or network needed
ruff check . ; ruff format --check .
cd ..\frontend ; npm run build    # typecheck + build
```

DB-backed tests use a separate `support_test` database on the Compose Postgres and Redis db 15 (override with `TEST_DATABASE_URL` / `TEST_REDIS_URL`); they skip if Postgres is unreachable, and **must not run in parallel** (they share it). Highlights: tenant isolation on every resource, SSRF and secret-handling suites, decision-loop policy (guards, confirmations, identity binding), workflows, hand-back and approvals, signed webhooks with retries, the job queue, scenario tests and the publish gate, retrieval quality on a golden set, budgets, retention, redaction, and the `/v1` contract snapshot. CI: [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Operational notes

- **Background work** (webhook delivery, document indexing, expiry and retention sweeps) runs on a Postgres job queue. Run at least one worker in production; more are safe.
- **Retention** is enforced by the housekeeping sweep (every few minutes) for *published* agents: conversations idle longer than the agent's `retention_days` are deleted with their messages and traces. Test conversations are removed after 3 days.
- **PII masking** is pattern based (emails, card numbers that pass the Luhn check, phone numbers). It is not a guarantee; use "don't store conversation text" when nothing may be kept.
- **Budgets** are soft: a turn already running finishes; new messages are refused with `429 budget_exceeded` until midnight UTC.
- Quality checks, query rewriting, reranking and test-question generation each make extra model calls; they are opt-in or on demand.
- Admins create users with an initial password (no email invitations yet). The legacy customer chat and ticket screens remain for the original demo; the platform direction does not depend on them.
