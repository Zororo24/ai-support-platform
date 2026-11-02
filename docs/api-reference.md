# API reference (`/v1`)

Base URL: your platform address. Every call needs `Authorization: Bearer <secret key>`; a key works for
exactly one agent (`/v1/agents/{agent_id}/…`). Call the API from your server only.

The API is additive: new fields and values can appear; existing ones do not change meaning. Ignore
fields you do not know.

## Errors

Every error has the same shape; switch on `code`, not on the message.

```json
{ "error": { "code": "rate_limited", "message": "Rate limit exceeded for this api key. Retry in 12 seconds." } }
```

| HTTP | `code` | Meaning |
|---|---|---|
| 401 | `missing_api_key`, `invalid_api_key` | No key, or unknown/revoked key |
| 403 | `agent_mismatch` | The key belongs to a different agent |
| 403 | `ip_not_allowed` | The key is limited to other addresses |
| 404 | `session_not_found`, `confirmation_not_found`, `tool_call_not_found` | Nothing matches (it may have expired) |
| 409 | `agent_not_published` | Publish the agent first |
| 409 | `session_busy` | Another message of this session is still being processed; retry |
| 409 | `session_user_mismatch` | That `session_id` belongs to a different `end_user.id` |
| 409 | `awaiting_tool_result` | The session is waiting for your result for an action (see below) |
| 409 | `request_in_progress` | An identical `Idempotency-Key` request is still running |
| 422 | `invalid_request`, `unknown_workflow`, `idempotency_key_reuse` | Bad input |
| 429 | `rate_limited`, `too_many_concurrent_requests` | Slow down; honour `Retry-After` |
| 429 | `budget_exceeded` | The agent's (or workspace's) daily AI budget is used up; resets at midnight UTC |
| 503 | `model_unavailable` | The AI provider is down; retry shortly (`Retry-After`) |

Rate-limit headroom comes back in `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.

## Send a message

`POST /v1/agents/{agent_id}/messages`

```json
{
  "message": "Where is my order ACM-1002?",
  "session_id": "chat-123",
  "end_user": { "id": "customer-42", "attributes": { "customer_id": "cust_42", "plan": "pro" } },
  "workflow": "return_request",
  "stream": false
}
```

* `message` (required, up to 8000 characters).
* `session_id`: your id for the conversation (letters, digits, `_ . : -`). Omit it to start a new one;
  the response returns the id.
* `end_user.id`: your customer id. `attributes` (up to 4 KB) are trusted facts from your system. They are
  filled into action requests and never shown to the AI unless the agent is configured to expose them.
* `workflow`: start a specific guided process (for example when the customer presses a "Start a return"
  button) instead of letting the agent decide.
* `stream: true` returns Server-Sent Events: `stage` events (`{"node","label"}`) while it works, then one
  `done` event with the response below (or `error`).
* `Idempotency-Key` header: retrying the same key with the same body returns the first response
  (`Idempotent-Replay: true`); not available with `stream`.

### The response

```json
{
  "run_id": "…",
  "session_id": "chat-123",
  "message_id": "…",
  "status": "completed",
  "outcome": "replied",
  "reply": "Your order shipped yesterday.",
  "citations": [{ "document_id": "…", "title": "Shipping", "snippet": "…" }],
  "actions_taken": [{ "name": "get_order", "effect": "read", "ok": true }],
  "pending_confirmation": null,
  "pending_approval": null,
  "action_request": null,
  "handoff": null,
  "usage": { "tokens": 812, "latency_ms": 1900 }
}
```

`status` tells your code what to do next:

| `status` | What it means | What you do |
|---|---|---|
| `completed` | The turn is done | Show `reply` |
| `needs_input` | A guided process is collecting details | Show `reply` (a question); send the answer as the next message |
| `needs_confirmation` | The customer must approve a change | Show `reply` with Yes/No; use `pending_confirmation.id` below. A plain "yes" typed as the next message also works |
| `pending_approval` | A person on your team must allow it | Show `reply`; the outcome arrives by `approval.decided` webhook |
| `action_required` | The agent needs **your backend** to do something | Perform `action_request`, then post the result (below) |
| `handed_off` | The agent is passing the customer to a person | Route them; see `handoff` (`reason`, `summary`, and `action_result` if the agent runs a hand-off action) |
| `failed` | The turn could not finish | Show `reply`; offer another route |

`outcome` is a finer-grained label for logs (`answered`, `refused`, `replied`, `workflow_completed`, …).
When it is `refused` the documents did not cover the question: a good moment to offer your own
escalation route.

## Confirmations

`POST /v1/agents/{agent_id}/confirmations`

```json
{ "session_id": "chat-123", "confirmation_id": "conf_…", "decision": "confirm" }
```

`decision` is `confirm` or `cancel`. The response is the usual message response. Confirmations expire
after 30 minutes.

## Actions your backend performs

For an action marked **Your own backend does it**, the platform never holds your credentials:

1. The message response has `status: "action_required"` and

   ```json
   "action_request": { "call_id": "call_…", "name": "issue_refund",
                        "arguments": { "order_number": "ACM-1002", "amount_cents": 1999 },
                        "expires_at": "…" }
   ```
   Arguments are already validated against the schema and rules you defined; customer identity comes
   from `end_user`, never from the AI.
2. Your server performs it with its own credentials.
3. `POST /v1/agents/{agent_id}/tool-results`

   ```json
   { "session_id": "chat-123", "call_id": "call_…", "result": { "refund_id": "RF-9", "status": "issued" } }
   ```
   or `{ "session_id": "…", "call_id": "…", "error": "card was already refunded" }`. The response is the
   agent's next message (same shape as above; it can itself ask for another action).

Until you answer, new messages for that session return `409 awaiting_tool_result`. Unanswered calls
expire after 15 minutes. Each `call_id` can be answered once. Results over 64 KB are refused.

## Sessions and privacy

* `GET /v1/agents/{agent_id}/sessions/{session_id}`: message count, and `active_run` if something is
  waiting (a confirmation, an approval, or an `action_request`).
* `DELETE /v1/agents/{agent_id}/sessions/{session_id}`: delete one conversation and everything stored for it.
* `DELETE /v1/agents/{agent_id}/end-users/{end_user_id}`: delete everything stored about a customer
  (`{"deleted_sessions": n}`).

How long conversations are kept, whether they are stored at all, and whether personal data is masked
before storing are set per agent under **Instructions & limits → Privacy**.

## Webhooks

Add endpoints under **Webhooks** in the dashboard. Each delivery is an HTTPS `POST` of

```json
{ "id": "evt_…", "type": "handoff.requested", "created_at": "…", "agent_id": "…", "data": { … } }
```

with headers `X-Webhook-Id` (same as `id`), `X-Webhook-Event`, `X-Webhook-Timestamp` and
`X-Webhook-Signature: t=<unix time>,v1=<hex hmac-sha256>`. The signature covers `"<t>.<raw body>"` using
your endpoint's signing secret (shown once when you create it).

**Verify every delivery** (the Python and Node clients have `verify_webhook` / `verifyWebhook`): recompute
the HMAC over the raw body, compare in constant time, and reject timestamps more than 5 minutes old.

Delivery is *at least once*: reply `2xx` quickly; failures are retried with growing delays for about a
day. De-duplicate on `id`. A failed delivery can be re-sent from the dashboard (same `id`).

| `type` | When | Useful `data` |
|---|---|---|
| `handoff.requested` | The agent hands a customer to a person | `session_id`, `end_user_id`, `handoff.reason`, `handoff.summary`, `handoff.action_result` |
| `approval.requested` | An action waits for staff | `approval_id`, `action`, `summary`, `session_id` |
| `approval.decided` | Staff approved or declined | `approved`, `decided_by`, `note`, `status`, `reply`, `action_request` (if a further step needs your backend) |
| `run.completed`, `run.failed` | A turn finished or failed | `run_id`, `session_id`, `outcome`, `reply` |
| `action.failed` | An action did not work | `action`, `error` |
| `budget.warning`, `budget.exceeded` | An agent used 80% / 100% of its daily AI budget | `used_tokens`, `daily_budget` |

Runs made in the dashboard (playground, tests) never send webhooks.

## Limits worth knowing

* Per key: requests per minute and concurrent requests; per customer: requests per minute (defaults 60,
  20 and a few concurrent; configurable per agent).
* One message per session is processed at a time.
* A key can be limited to an IP allow-list (dashboard: Integration → key → allowed addresses).
  Behind a reverse proxy set `TRUST_PROXY_HEADERS=true` so the real caller address is used.
* An agent can have a daily token budget; a workspace-wide ceiling is `TENANT_DAILY_TOKEN_BUDGET`.
