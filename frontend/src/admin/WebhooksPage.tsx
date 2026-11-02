import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  createWebhook,
  deleteWebhook,
  listAgents,
  listWebhookDeliveries,
  listWebhookEvents,
  listWebhooks,
  patchWebhook,
  replayWebhookDelivery,
  testWebhook,
} from "../api/client";
import type { AgentSummary, WebhookDelivery, WebhookEndpoint } from "../types";
import { formatDate } from "./format";

const EVENT_LABELS: Record<string, string> = {
  "handoff.requested": "A customer needs a person",
  "approval.requested": "Staff approval is needed",
  "approval.decided": "Staff approved or declined a request",
  "run.completed": "A conversation step finished",
  "run.failed": "A conversation step failed",
  "action.failed": "An action did not work",
};

export function WebhooksPage() {
  const [items, setItems] = useState<WebhookEndpoint[] | null>(null);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [events, setEvents] = useState<string[]>([]);
  const [creating, setCreating] = useState(false);
  const [reveal, setReveal] = useState<{ url: string; secret: string } | null>(null);
  const [openLog, setOpenLog] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await listWebhooks());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load webhooks");
    }
  }, []);

  useEffect(() => {
    void load();
    listAgents().then(setAgents).catch(() => undefined);
    listWebhookEvents().then(setEvents).catch(() => undefined);
  }, [load]);

  const agentName = (id: string | null) => (id ? (agents.find((a) => a.id === id)?.name ?? "Unknown agent") : "All agents");

  async function toggle(hook: WebhookEndpoint) {
    try {
      await patchWebhook(hook.id, { active: !hook.active });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update the webhook");
    }
  }

  async function rotate(hook: WebhookEndpoint) {
    if (!window.confirm("Make a new signing secret? Your receiver must be updated, or it will reject our events.")) return;
    try {
      const updated = await patchWebhook(hook.id, { rotate_secret: true });
      if (updated.secret) setReveal({ url: hook.url, secret: updated.secret });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not rotate the secret");
    }
  }

  async function remove(hook: WebhookEndpoint) {
    if (!window.confirm(`Stop sending events to ${hook.url}? Its delivery history is deleted.`)) return;
    try {
      await deleteWebhook(hook.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not delete the webhook");
    }
  }

  return (
    <div>
      <div className="toolbar">
        <p className="hint" style={{ flex: 1, margin: 0 }}>
          Webhooks tell your own systems what happened: a customer needs a person, a request is waiting for
          staff approval, an action failed. Each event is signed so you can check it came from us, and is
          retried for about a day if your server is down.
        </p>
        <button className="primary" onClick={() => setCreating(!creating)}>
          {creating ? "Close" : "Add webhook"}
        </button>
      </div>

      {reveal && (
        <div className="card">
          <h3>Signing secret for {reveal.url}</h3>
          <p className="hint">Copy it now. For safety it can&apos;t be shown again (you can make a new one later).</p>
          <code style={{ wordBreak: "break-all" }}>{reveal.secret}</code>
          <div className="actions">
            <button onClick={() => void navigator.clipboard?.writeText(reveal.secret)}>Copy</button>
            <button onClick={() => setReveal(null)}>I&apos;ve saved it</button>
          </div>
        </div>
      )}

      {creating && (
        <WebhookForm
          agents={agents}
          events={events}
          onCreated={(hook) => {
            setCreating(false);
            if (hook.secret) setReveal({ url: hook.url, secret: hook.secret });
            void load();
          }}
          onCancel={() => setCreating(false)}
        />
      )}
      {error && <p className="error">{error}</p>}

      {items === null ? (
        <p>Loading…</p>
      ) : items.length === 0 ? (
        <p className="hint">No webhooks yet. Add one to get handoffs and approvals delivered to your systems.</p>
      ) : (
        items.map((hook) => (
          <div className="card" key={hook.id} style={{ opacity: hook.active ? 1 : 0.65 }}>
            <div className="doc-head">
              <div>
                <strong>
                  <code>{hook.url}</code>
                </strong>
                {hook.description && <div className="hint">{hook.description}</div>}
                <div className="hint">
                  {agentName(hook.agent_id)} · {hook.events.includes("*") ? "All events" : hook.events.map((e) => EVENT_LABELS[e] ?? e).join(", ")}{" "}
                  · secret {hook.secret_hint}
                </div>
              </div>
              <div className="row-actions">
                <TestButton hook={hook} onDone={() => setOpenLog(hook.id)} />
                <button onClick={() => setOpenLog(openLog === hook.id ? null : hook.id)}>
                  {openLog === hook.id ? "Hide history" : "History"}
                </button>
                <button onClick={() => void toggle(hook)}>{hook.active ? "Pause" : "Resume"}</button>
                <button onClick={() => void rotate(hook)}>New secret</button>
                <button className="danger" onClick={() => void remove(hook)}>
                  Delete
                </button>
              </div>
            </div>
            {openLog === hook.id && <DeliveryLog hook={hook} />}
          </div>
        ))
      )}
    </div>
  );
}

function TestButton({ hook, onDone }: { hook: WebhookEndpoint; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await testWebhook(hook.id);
        } finally {
          setBusy(false);
          onDone();
        }
      }}
    >
      {busy ? "Sending…" : "Send test"}
    </button>
  );
}

function DeliveryLog({ hook }: { hook: WebhookEndpoint }) {
  const [rows, setRows] = useState<WebhookDelivery[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await listWebhookDeliveries(hook.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the history");
    }
  }, [hook.id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function replay(row: WebhookDelivery) {
    try {
      await replayWebhookDelivery(hook.id, row.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not resend");
    }
  }

  if (error) return <p className="error">{error}</p>;
  if (rows === null) return <p>Loading…</p>;
  if (rows.length === 0) return <p className="hint">Nothing has been sent to this address yet.</p>;
  return (
    <table className="doc-table">
      <thead>
        <tr>
          <th>Event</th>
          <th>When</th>
          <th>Result</th>
          <th>Tries</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id} className="static">
            <td>
              <code>{r.event_type}</code>
              <div className="hint">{r.event_id}</div>
            </td>
            <td>{formatDate(r.created_at)}</td>
            <td>
              <span className={`badge ${r.status === "delivered" ? "doc-ready" : r.status === "failed" ? "doc-failed" : "doc-pending"}`}>
                {r.status === "queued" ? "Retrying" : r.status}
              </span>{" "}
              {r.response_code !== null && <small>HTTP {r.response_code}</small>}
              {r.last_error && r.status !== "delivered" && <div className="hint">{r.last_error}</div>}
            </td>
            <td>{r.attempts}</td>
            <td className="row-actions">
              <button onClick={() => void replay(r)}>Send again</button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function WebhookForm({
  agents,
  events,
  onCreated,
  onCancel,
}: {
  agents: AgentSummary[];
  events: string[];
  onCreated: (hook: WebhookEndpoint) => void;
  onCancel: () => void;
}) {
  const [url, setUrl] = useState("https://");
  const [description, setDescription] = useState("");
  const [agentId, setAgentId] = useState("");
  const [all, setAll] = useState(true);
  const [chosen, setChosen] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(
        await createWebhook({
          url: url.trim(),
          description: description.trim(),
          agent_id: agentId || null,
          events: all ? ["*"] : chosen,
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not add the webhook");
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={onSubmit}>
      <h3>New webhook</h3>
      <div className="user-form">
        <label style={{ flex: 1, minWidth: "18rem" }}>
          Address on your server (https)
          <input value={url} onChange={(e) => setUrl(e.target.value)} maxLength={500} required />
        </label>
        <label>
          Which agent?
          <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        <label style={{ flex: 1, minWidth: "12rem" }}>
          Note (optional)
          <input value={description} onChange={(e) => setDescription(e.target.value)} maxLength={200} placeholder="Help desk" />
        </label>
      </div>
      <fieldset style={{ border: "none", padding: 0 }}>
        <label className="inline">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> Send every kind of event
        </label>
        {!all &&
          events.map((e) => (
            <label key={e} className="inline" style={{ display: "block" }}>
              <input
                type="checkbox"
                checked={chosen.includes(e)}
                onChange={(ev) => setChosen(ev.target.checked ? [...chosen, e] : chosen.filter((c) => c !== e))}
              />{" "}
              {EVENT_LABELS[e] ?? e} <small>({e})</small>
            </label>
          ))}
      </fieldset>
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy || (!all && chosen.length === 0)}>
          {busy ? "Adding…" : "Add webhook"}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
