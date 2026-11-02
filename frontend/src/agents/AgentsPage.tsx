import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { createAgent, listAgents } from "../api/client";
import { formatDate } from "../admin/format";
import type { AgentSummary } from "../types";
import { AgentDetailPage } from "./AgentDetailPage";

function StatusPill({ agent }: { agent: AgentSummary }) {
  if (agent.published_version === null) return <span className="badge doc-pending">Not published</span>;
  return (
    <>
      <span className="badge doc-ready">Live v{agent.published_version}</span>
      {agent.has_unpublished_changes && <span className="badge doc-pending">Unpublished changes</span>}
    </>
  );
}

function NewAgentForm({ onCreated, onCancel }: { onCreated: (id: string) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [template, setTemplate] = useState<"support" | "blank">("support");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await createAgent(name.trim(), description.trim(), template);
      onCreated(created.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the agent");
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={onSubmit}>
      <h3>New agent</h3>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} />
      </label>
      <label>
        What is it for? (optional)
        <input value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} />
      </label>
      <label>
        Start from
        <select value={template} onChange={(e) => setTemplate(e.target.value as "support" | "blank")}>
          <option value="support">Support assistant (answers from your documents)</option>
          <option value="blank">Blank (no knowledge attached)</option>
        </select>
      </label>
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy || !name.trim()}>
          {busy ? "Creating…" : "Create agent"}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function AgentsPage() {
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setAgents(await listAgents());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load agents");
    }
  }, []);

  useEffect(() => {
    if (openId === null) void load();
  }, [load, openId]);

  if (openId !== null) {
    return <AgentDetailPage id={openId} onBack={() => setOpenId(null)} />;
  }

  return (
    <div>
      <div className="toolbar">
        <p className="hint" style={{ flex: 1, margin: 0 }}>
          An agent is a chatbot brain your own website or app talks to through the API: its knowledge,
          instructions and limits live here.
        </p>
        <button className="primary" onClick={() => setCreating((v) => !v)}>
          {creating ? "Close" : "New agent"}
        </button>
      </div>
      {creating && (
        <NewAgentForm
          onCancel={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            setOpenId(id);
          }}
        />
      )}
      {error && <p className="error">{error}</p>}
      {agents === null ? (
        <p>Loading…</p>
      ) : agents.length === 0 ? (
        <p className="hint">No agents yet. Create your first one.</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Status</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => (
              <tr
                key={a.id}
                tabIndex={0}
                onClick={() => setOpenId(a.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") setOpenId(a.id);
                }}
              >
                <td>
                  <strong>{a.name}</strong>
                  {a.description && <div className="hint">{a.description}</div>}
                </td>
                <td>
                  <StatusPill agent={a} />
                </td>
                <td>{formatDate(a.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
