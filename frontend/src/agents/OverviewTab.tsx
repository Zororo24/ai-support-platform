import { useRef, useState } from "react";
import type { FormEvent } from "react";

import { getRun, playgroundConfirm, playgroundMessage, recordScenario } from "../api/client";
import { RunTrace } from "../runs/RunTrace";
import type { AgentDetail, AgentReply, RunDetailInfo } from "../types";

type Line = { role: "user" | "agent"; text: string; reply?: AgentReply };

export function OverviewTab({
  agent,
  dirty,
  busy,
  onRename,
}: {
  agent: AgentDetail;
  dirty: boolean;
  busy: boolean;
  onRename: (name: string, description: string) => Promise<void>;
}) {
  const [name, setName] = useState(agent.name);
  const [description, setDescription] = useState(agent.description);

  const [version, setVersion] = useState<"draft" | "published">("draft");
  const [mock, setMock] = useState(false);
  const [workflow, setWorkflow] = useState("");
  const [attributes, setAttributes] = useState('{\n  "customer_id": "cust_1"\n}');
  const [lines, setLines] = useState<Line[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trace, setTrace] = useState<RunDetailInfo | null>(null);
  const [savedTest, setSavedTest] = useState<string | null>(null);
  const sessionRef = useRef<string | null>(null);

  async function showTrace(runId: string | null) {
    if (!runId) return;
    try {
      setTrace(await getRun(runId));
    } catch {
      /* the trace is a convenience; the chat still works */
    }
  }

  const hasActions = agent.draft_config.actions.length > 0;
  const processes = agent.draft_config.workflows.filter((w) => w.enabled);

  function parseAttributes(): Record<string, unknown> | null {
    if (!attributes.trim()) return {};
    try {
      const parsed: unknown = JSON.parse(attributes);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
    } catch {
      /* fall through */
    }
    setError("Customer attributes must be a JSON object, e.g. {\"customer_id\": \"cust_1\"}");
    return null;
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || sending) return;
    const attrs = parseAttributes();
    if (attrs === null) return;
    setDraft("");
    setError(null);
    setSending(true);
    setLines((prev) => [...prev, { role: "user", text: message }]);
    try {
      const reply = await playgroundMessage(agent.id, message, sessionRef.current, version, {
        mockActions: mock,
        attributes: attrs,
        workflow: workflow && !sessionRef.current ? workflow : undefined,
      });
      sessionRef.current = reply.session_id;
      setLines((prev) => [...prev, { role: "agent", text: reply.reply, reply }]);
      void showTrace(reply.run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "The test message failed");
      setDraft(message);
    } finally {
      setSending(false);
    }
  }

  async function decide(confirmationId: string, decision: "confirm" | "cancel") {
    if (!sessionRef.current || sending) return;
    setSending(true);
    setError(null);
    try {
      const reply = await playgroundConfirm(agent.id, sessionRef.current, confirmationId, decision, version, mock);
      setLines((prev) => [
        ...prev,
        { role: "user", text: decision === "confirm" ? "✓ Approved" : "✗ Declined" },
        { role: "agent", text: reply.reply, reply },
      ]);
      void showTrace(reply.run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not record the decision");
    } finally {
      setSending(false);
    }
  }

  function reset() {
    sessionRef.current = null;
    setLines([]);
    setError(null);
    setTrace(null);
    setSavedTest(null);
  }

  async function saveAsTest() {
    if (!sessionRef.current) return;
    const first = lines.find((l) => l.role === "user")?.text ?? "Conversation";
    const name = window.prompt("Name this test", first.slice(0, 60));
    if (!name?.trim()) return;
    setError(null);
    try {
      await recordScenario(agent.id, sessionRef.current, name.trim());
      setSavedTest(name.trim());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the test");
    }
  }

  const lastPending = [...lines].reverse().find((l) => l.reply)?.reply?.pending_confirmation ?? null;

  return (
    <div className="overview">
      <div className="card">
        <h3>Details</h3>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
        </label>
        <label>
          Description
          <input value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} />
        </label>
        <button
          disabled={busy || !name.trim() || (name === agent.name && description === agent.description)}
          onClick={() => void onRename(name.trim(), description.trim())}
        >
          Save details
        </button>
      </div>

      <div className="card">
        <div className="doc-head">
          <h3 style={{ margin: 0 }}>Try it</h3>
          <select
            aria-label="Version to test"
            value={version}
            onChange={(e) => {
              setVersion(e.target.value as "draft" | "published");
              reset();
            }}
          >
            <option value="draft">Draft (your latest saved edits)</option>
            <option value="published" disabled={agent.published_version === null}>
              Published{agent.published_version !== null ? ` (v${agent.published_version})` : ""}
            </option>
          </select>
          <button onClick={() => void saveAsTest()} disabled={lines.length === 0 || sending} title="Keep this conversation as a test that runs before every publish">
            Save as a test
          </button>
          <button onClick={reset} disabled={lines.length === 0}>
            Clear
          </button>
        </div>
        {version === "draft" && dirty && <p className="hint">You have unsaved edits. Save the draft to test them.</p>}
        {processes.length > 0 && (
          <label>
            Start a conversation with
            <select
              value={workflow}
              onChange={(e) => {
                setWorkflow(e.target.value);
                reset();
              }}
              style={{ maxWidth: "24rem" }}
            >
              <option value="">Let the agent decide</option>
              {processes.map((w) => (
                <option key={w.name} value={w.name}>
                  Process: {w.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {hasActions && (
          <div className="user-form">
            <label className="inline">
              <input type="checkbox" checked={mock} onChange={(e) => setMock(e.target.checked)} />
              Use sample responses (don&apos;t call my systems)
            </label>
            <label style={{ flex: 1, minWidth: "14rem" }}>
              Customer attributes your system would send
              <textarea rows={3} value={attributes} onChange={(e) => setAttributes(e.target.value)} />
            </label>
          </div>
        )}
        <div className="messages playground">
          {lines.length === 0 && <p className="hint">Ask something a customer would ask.</p>}
          {lines.map((line, i) => (
            <div key={i} className={`bubble ${line.role === "user" ? "user" : "assistant"}`}>
              <p>{line.text}</p>
              {line.reply && line.reply.actions_taken.length > 0 && (
                <div className="chips">
                  {line.reply.actions_taken.map((a, j) => (
                    <span key={j} className={`badge ${a.ok ? "doc-ready" : "doc-failed"}`}>
                      {a.name}
                      {a.ok ? "" : ` failed (${a.error ?? "error"})`}
                    </span>
                  ))}
                </div>
              )}
              {line.reply && line.reply.citations.length > 0 && (
                <div className="attachment citations">
                  <strong>Sources</strong>
                  <ul>
                    {line.reply.citations.map((c, j) => (
                      <li key={j}>
                        <em>{c.title}</em>: {c.snippet}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {line.reply?.handoff && (
                <div className="attachment">
                  <strong>Handed off</strong> {line.reply.handoff.summary}
                </div>
              )}
              {line.reply && (
                <small className="hint">
                  {line.reply.status === "needs_confirmation" ? "waiting for approval" : line.reply.outcome} ·{" "}
                  {line.reply.usage.latency_ms} ms · {line.reply.usage.tokens} tokens
                </small>
              )}
            </div>
          ))}
          {sending && <p className="hint">Thinking…</p>}
        </div>
        {lastPending && lines[lines.length - 1]?.reply?.pending_confirmation && (
          <div className="secret-box">
            <strong>Waiting for the customer: {lastPending.summary}</strong>
            <div className="actions">
              <button className="primary" disabled={sending} onClick={() => void decide(lastPending.id, "confirm")}>
                Approve
              </button>
              <button disabled={sending} onClick={() => void decide(lastPending.id, "cancel")}>
                Decline
              </button>
            </div>
            <small className="hint">Your customers would see Yes/No buttons here, or could just reply.</small>
          </div>
        )}
        {error && <p className="error">{error}</p>}
        {savedTest && (
          <p className="notice">
            Saved as the test “{savedTest}”. Open the <strong>Tests</strong> tab to check what it expects.
          </p>
        )}
        <form className="composer" onSubmit={send}>
          <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Type a test message…" />
          <button type="submit" className="primary" disabled={sending || !draft.trim()}>
            Send
          </button>
        </form>
      </div>
      {trace && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>What just happened</h3>
          <RunTrace run={trace} />
        </div>
      )}
    </div>
  );
}
