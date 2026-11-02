# Quickstart: your first agent answering over the API

You will create an agent in the dashboard, give it some knowledge, publish it, and call it from your
own server. About ten minutes once the platform is running.

> The platform is the **agent**. You keep your own chatbot or website: your backend sends each
> customer message to the API and shows what comes back. There is no end-user screen to build or
> embed.

## 0. Run the platform

Follow "Running locally" in the [README](../README.md) (database, `alembic upgrade head`, seed data,
API on `:8000`, dashboard on `:5173`). You need an AI provider key in `.env` (`LLM_API_KEY`) for
real answers.

Sign in to the dashboard as `acme.admin@example.com` (password from your seed settings).

## 1. Create an agent

**Agents → New agent.** Pick the *Support assistant* template. Under **Instructions** you can describe
its tone and what it should and should not do.

## 2. Give it knowledge

**Knowledge base → Upload** a Markdown, TXT, PDF or DOCX file (or paste text), and wait until its
status says *Ready*. Then open the agent's **Knowledge** tab and tick the collection that holds it.

Check it with the **Overview & test** tab: ask a question your document answers. The panel under the
chat shows exactly what the agent did (**What just happened**).

## 3. Publish

Press **Publish…**. Publishing freezes the current settings as a numbered version; your API always
serves the published version, so you can keep editing the draft safely. (If you have written
[tests](#tests-and-safe-publishing), they run first.)

## 4. Make a key and call it

On the **Integration** tab, create a key. It starts with `sk_live_` and is shown once; store it as a
secret on your server. **Never put it in website or app code.**

```bash
curl -X POST http://localhost:8000/v1/agents/$AGENT_ID/messages \
  -H "Authorization: Bearer $AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "message": "How long do I have to return an item?",
        "session_id": "chat-123",
        "end_user": { "id": "customer-42", "attributes": { "customer_id": "cust_42" } }
      }'
```

```json
{
  "session_id": "chat-123",
  "status": "completed",
  "outcome": "answered",
  "reply": "You can return items within 30 days of delivery.",
  "citations": [{ "title": "Return policy", "snippet": "…" }]
}
```

* `session_id` is yours: reuse it for every message of one conversation.
* `end_user.id` is your own customer id. `end_user.attributes` are facts about the customer **from your
  system** (for example `customer_id`). Actions use them to look up the right records; the AI cannot
  change or invent them.

From Python or Node, use the thin clients in [`clients/`](../clients/README.md):

```python
from agent_platform import AgentPlatform
agents = AgentPlatform(api_key=KEY, agent_id=AGENT_ID, base_url="http://localhost:8000")
reply = agents.send("How long do I have to return an item?", session_id="chat-123", end_user_id="customer-42")
print(reply.status, reply.reply)
```

A runnable website with a chat box is in [`examples/chat-site`](../examples/chat-site).

## 5. Let it do things in your systems

Answers from documents are only half of it. **Connections** says where one of your systems lives and how
to sign in to it (credentials are stored encrypted and never shown again). The agent's **Actions** tab
defines what the agent may do there: look up an order, open a ticket, start a return. Each action has a
form for its inputs, the request to send, safety rules (*block refunds over $200*), and a **Test** button.

Anything that **changes** something asks the customer to confirm first, or a person on your team to
approve it (**Approvals**). Guided multi-step jobs (collect the order number, check eligibility, confirm,
create the return) are built in **Processes** from templates.

If you would rather not give the platform credentials at all, mark an action **Your own backend does it**.
The API response then tells your server what to do (`status: "action_required"`), your server does it,
and you post the result back. See the [API reference](api-reference.md#actions-your-backend-performs).

## 6. Hear about hand-offs and approvals

**Webhooks → Add webhook** with an https address on your server. Choose the events you care about
(`handoff.requested`, `approval.requested`, `approval.decided`, `action.failed`, `budget.warning`…).
Every event is signed (`X-Webhook-Signature`); the clients include `verify_webhook`. **Send test**
shows the whole round trip.

## Tests and safe publishing

Save a good conversation from the playground with **Save as a test**. Tests assert behaviour (actions
used, actions refused, how it ends), run against sample responses and never touch your real systems. They
run before every publish; a failing test blocks it. **Versions** shows what changed since the live
version and lets you roll back with one click.

## What to do next

* Read the [API reference](api-reference.md): every status, error code, and webhook.
* Set limits: per-key rate limits, a daily token budget, and an IP allow-list for each key.
* Run **Knowledge base → Quality** to measure whether search finds the right passages.
