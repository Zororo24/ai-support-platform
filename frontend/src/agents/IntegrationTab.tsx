import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { createApiKey, listApiKeys, revokeApiKey, setApiKeyAllowedIps } from "../api/client";
import { formatDate } from "../admin/format";
import type { AgentDetail, ApiKeyCreated, ApiKeyInfo } from "../types";

function exampleCurl(agentId: string): string {
  const base = window.location.origin;
  return [
    `curl -X POST ${base}/v1/agents/${agentId}/messages \\`,
    `  -H "Authorization: Bearer $AGENT_API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"session_id": "chat-123", "end_user": {"id": "customer-42"}, "message": "What is your return policy?"}'`,
  ].join("\n");
}

export function IntegrationTab({ agent }: { agent: AgentDetail }) {
  const [keys, setKeys] = useState<ApiKeyInfo[] | null>(null);
  const [name, setName] = useState("");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setKeys(await listApiKeys(agent.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load keys");
    }
  }, [agent.id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function onCreate(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setCreated(await createApiKey(agent.id, name.trim()));
      setCopied(false);
      setName("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the key");
    } finally {
      setBusy(false);
    }
  }

  async function limitAddresses(key: ApiKeyInfo) {
    const answer = window.prompt(
      "Only these addresses may use this key (comma separated, ranges like 203.0.113.0/24 allowed). Leave empty to allow any address.",
      key.allowed_ips.join(", "),
    );
    if (answer === null) return;
    setError(null);
    try {
      await setApiKeyAllowedIps(
        agent.id,
        key.id,
        answer
          .split(",")
          .map((a) => a.trim())
          .filter(Boolean),
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the addresses");
    }
  }

  async function revoke(key: ApiKeyInfo) {
    if (!window.confirm(`Revoke “${key.name}” (${key.prefix}…)? Anything using it stops working immediately.`)) return;
    setError(null);
    try {
      await revokeApiKey(agent.id, key.id);
      if (created?.id === key.id) setCreated(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not revoke the key");
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="integration">
      <div className="card">
        <h3>API keys</h3>
        <p className="hint">
          Your server uses a secret key to send customer messages to this agent. <strong>Never put a secret key
          in website or app code</strong>; call the API from your backend.
        </p>
        <form onSubmit={onCreate} className="user-form">
          <label>
            Key name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. production backend"
              maxLength={120}
              required
            />
          </label>
          <button type="submit" className="primary" disabled={busy || !name.trim()}>
            Create key
          </button>
        </form>
        {error && <p className="error">{error}</p>}

        {created && (
          <div className="secret-box">
            <strong>Copy this key now. It won&apos;t be shown again.</strong>
            <code>{created.key}</code>
            <button onClick={() => void copy(created.key)}>{copied ? "Copied" : "Copy"}</button>
          </div>
        )}

        {keys === null ? (
          <p>Loading…</p>
        ) : keys.length === 0 ? (
          <p className="hint">No keys yet.</p>
        ) : (
          <table className="doc-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Key</th>
                <th>Created</th>
                <th>Last used</th>
                <th>Allowed from</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id} className="static">
                  <td>{k.name}</td>
                  <td>
                    <code>{k.prefix}…</code>
                  </td>
                  <td>{formatDate(k.created_at)}</td>
                  <td>{k.last_used_at ? formatDate(k.last_used_at) : "Never"}</td>
                  <td>
                    {k.allowed_ips.length ? <small>{k.allowed_ips.join(", ")}</small> : <small>Anywhere</small>}{" "}
                    {!k.revoked_at && <button onClick={() => void limitAddresses(k)}>Change</button>}
                  </td>
                  <td>
                    {k.revoked_at ? (
                      <span className="badge doc-failed">Revoked</span>
                    ) : (
                      <button className="danger" onClick={() => void revoke(k)}>
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h3>Send a message</h3>
        {agent.published_version === null && (
          <p className="error">This agent isn&apos;t published yet, so the API will answer with an error.</p>
        )}
        <pre className="code">{exampleCurl(agent.id)}</pre>
        <p className="hint">
          Reuse the same <code>session_id</code> for one conversation. <code>end_user.id</code> is your own
          customer ID. Add header <code>Idempotency-Key</code> to make retries safe, or send{" "}
          <code>&quot;stream&quot;: true</code> for live progress events.
        </p>
        <pre className="code">{`{
  "session_id": "chat-123",
  "status": "completed",
  "outcome": "answered",
  "reply": "You can return items within 30 days...",
  "citations": [{ "title": "Return policy", "snippet": "..." }],
  "usage": { "tokens": 812, "latency_ms": 1900 }
}`}</pre>
        <p className="hint">
          When <code>outcome</code> is <code>refused</code>, the documents didn&apos;t cover the question. A good
          moment to offer your own escalation route.
        </p>
      </div>

      <div className="card">
        <h3>What your chatbot has to handle</h3>
        <p className="hint">
          Every response has a <code>status</code>. Most are just text to show; three need something from your
          code.
        </p>
        <table className="doc-table">
          <tbody>
            <tr className="static">
              <td>
                <code>completed</code>
              </td>
              <td>Show <code>reply</code>.</td>
            </tr>
            <tr className="static">
              <td>
                <code>needs_input</code>
              </td>
              <td>The agent is collecting details for a process. Show <code>reply</code> (a question) and send the answer as the next message.</td>
            </tr>
            <tr className="static">
              <td>
                <code>needs_confirmation</code>
              </td>
              <td>
                Show <code>reply</code> with Yes/No buttons, then <code>POST /v1/agents/{"{id}"}/confirmations</code>{" "}
                with <code>pending_confirmation.id</code>.
              </td>
            </tr>
            <tr className="static">
              <td>
                <code>pending_approval</code>
              </td>
              <td>
                A person on your team must allow the action. Show <code>reply</code>. You&apos;ll get{" "}
                <code>approval.decided</code> by webhook with the final text to send.
              </td>
            </tr>
            <tr className="static">
              <td>
                <code>action_required</code>
              </td>
              <td>
                Your backend performs <code>action_request</code> (<code>name</code>, <code>arguments</code>), then
                sends the outcome to <code>POST /v1/agents/{"{id}"}/tool-results</code>. The response is the
                agent&apos;s next message.
              </td>
            </tr>
            <tr className="static">
              <td>
                <code>handed_off</code>
              </td>
              <td>
                Route the customer to a person. <code>handoff</code> has the reason and summary (and the result of
                your handoff action, if you set one).
              </td>
            </tr>
          </tbody>
        </table>
        <pre className="code">{`curl -X POST $BASE/v1/agents/${agent.id}/tool-results \\
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \\
  -d '{"session_id": "chat-123", "call_id": "call_…",
       "result": {"refund_id": "RF-9", "status": "issued"}}'
# or, if it failed:  "error": "card was already refunded"`}</pre>
        <p className="hint">
          Under <strong>Webhooks</strong> you can receive handoffs, approval requests and failures on your own
          server, signed with <code>X-Webhook-Signature</code>. Under <strong>Approvals</strong> your team
          allows or declines actions marked as needing staff approval.
        </p>
      </div>
    </div>
  );
}
