# Clients

Thin wrappers over the `/v1` HTTP API. They add no behaviour of their own beyond error mapping and
webhook-signature checking, so anything they do you can do with plain `curl` (see the
[API reference](../docs/api-reference.md)).

| | File | Needs |
|---|---|---|
| Python | [`python/agent_platform.py`](python/agent_platform.py) | `httpx` |
| Node 18+ | [`node/agent-platform.mjs`](node/agent-platform.mjs) | nothing |

Both read the same way:

```python
agents = AgentPlatform(api_key=KEY, agent_id=AGENT_ID, base_url="https://agents.example.com")
reply = agents.send("Where is my order?", session_id="chat-1", end_user_id="cust-42",
                    end_user_attributes={"customer_id": "cust_42"})

if reply.status == "needs_confirmation":
    reply = agents.confirm(reply.session_id, reply.pending_confirmation["id"], approve=True)
elif reply.status == "action_required":
    outcome = do_it(reply.action_request["name"], reply.action_request["arguments"])  # your code
    reply = agents.send_tool_result(reply.session_id, reply.action_request["call_id"], result=outcome)
```

Run them on your server. The key is a secret: never ship it to a browser or mobile app.

Webhook receivers: `verify_webhook(secret, headers["X-Webhook-Signature"], raw_body)` (Python) or
`verifyWebhook(secret, signature, rawBody)` (Node). Pass the **raw** body bytes, not re-serialised JSON.
