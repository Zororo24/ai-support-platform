// A thin Node client for the agent platform's /v1 API (Node 18+, no dependencies).
//
//   import { AgentPlatform } from "./agent-platform.mjs";
//   const agents = new AgentPlatform({ apiKey: process.env.AGENT_API_KEY, agentId: "...", baseUrl: "https://agents.example.com" });
//   const reply = await agents.send("Where is my order ACM-1002?", { sessionId: "chat-123", endUserId: "customer-42",
//                                                                    endUserAttributes: { customer_id: "cust_42" } });
//
// Run it on YOUR SERVER: the key is a secret and must never reach a browser or app.
//
// reply.status tells your code what to do next:
//   completed            show reply.reply
//   needs_input          the agent is collecting details; send the customer's answer as the next message
//   needs_confirmation   show Yes/No, then agents.confirm(sessionId, reply.pending_confirmation.id, true|false)
//   pending_approval     staff must allow it; wait for the `approval.decided` webhook
//   action_required      do reply.action_request with your own credentials, then agents.sendToolResult(...)
//   handed_off           route the customer to a person (reply.handoff)

import { createHmac, timingSafeEqual } from "node:crypto";

export class AgentPlatformError extends Error {
  constructor(status, code, message, retryAfter) {
    super(`${code}: ${message}`);
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter ?? null;
  }
}

export class AgentPlatform {
  constructor({ apiKey, agentId, baseUrl = "http://localhost:8000", fetch: fetchImpl = globalThis.fetch }) {
    this.agentId = agentId;
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.headers = { Authorization: `Bearer ${apiKey}` };
    this.fetch = fetchImpl;
  }

  send(message, { sessionId, endUserId, endUserAttributes, workflow, idempotencyKey } = {}) {
    const body = { message };
    if (sessionId) body.session_id = sessionId;
    if (endUserId) body.end_user = { id: endUserId, attributes: endUserAttributes ?? {} };
    if (workflow) body.workflow = workflow;
    return this.#call("POST", "messages", body, idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {});
  }

  confirm(sessionId, confirmationId, approve) {
    return this.#call("POST", "confirmations", {
      session_id: sessionId,
      confirmation_id: confirmationId,
      decision: approve ? "confirm" : "cancel",
    });
  }

  // Return what your system did for an `action_required` reply. Pass `error` if it failed.
  sendToolResult(sessionId, callId, { result, error } = {}) {
    const body = { session_id: sessionId, call_id: callId };
    if (error !== undefined) body.error = error;
    else body.result = result ?? null;
    return this.#call("POST", "tool-results", body);
  }

  session(sessionId) {
    return this.#call("GET", `sessions/${encodeURIComponent(sessionId)}`);
  }

  async deleteSession(sessionId) {
    await this.#call("DELETE", `sessions/${encodeURIComponent(sessionId)}`);
  }

  // Erase everything stored about one customer. Resolves to the number of sessions deleted.
  async deleteEndUser(endUserId) {
    return (await this.#call("DELETE", `end-users/${encodeURIComponent(endUserId)}`)).deleted_sessions;
  }

  async #call(method, tail, body, extraHeaders = {}) {
    const response = await this.fetch(`${this.baseUrl}/v1/agents/${this.agentId}/${tail}`, {
      method,
      headers: { ...this.headers, ...(body ? { "Content-Type": "application/json" } : {}), ...extraHeaders },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
      let code = "http_error";
      let message = response.statusText;
      try {
        ({ code, message } = (await response.json()).error);
      } catch {
        /* not the API's error shape */
      }
      const retry = Number(response.headers.get("Retry-After"));
      throw new AgentPlatformError(response.status, code, message, Number.isFinite(retry) && retry > 0 ? retry : null);
    }
    return response.status === 204 ? null : response.json();
  }
}

// Check an incoming webhook: the HMAC matches AND the timestamp is recent (stops replays).
// `signatureHeader` is X-Webhook-Signature; `body` is the RAW request body (string or Buffer).
export function verifyWebhook(secret, signatureHeader, body, { tolerance = 300, now = Date.now() / 1000 } = {}) {
  const parts = Object.fromEntries(
    String(signatureHeader ?? "")
      .split(",")
      .map((item) => item.split(/=(.*)/s).slice(0, 2)),
  );
  const timestamp = Number(parts.t);
  if (!Number.isFinite(timestamp) || !parts.v1 || Math.abs(now - timestamp) > tolerance) return false;
  const expected = createHmac("sha256", secret).update(`${timestamp}.`).update(body).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(parts.v1);
  return a.length === b.length && timingSafeEqual(a, b);
}
