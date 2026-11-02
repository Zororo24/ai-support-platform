import { useCallback, useEffect, useState } from "react";

import { listAgents, listRuns, getRun } from "../api/client";
import { formatDate } from "../admin/format";
import type { AgentSummary, RunDetailInfo, RunSummary } from "../types";
import { ReplayPanel } from "./ReplayPanel";
import { RunTrace, statusBadgeClass } from "./RunTrace";

const PAGE_SIZE = 25;

const STATUSES = ["completed", "waiting_confirmation", "handed_off", "failed", "cancelled", "expired"];

export function RunsPage() {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentId, setAgentId] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [endUser, setEndUser] = useState("");
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<RunSummary[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetailInfo | null>(null);

  useEffect(() => {
    listAgents()
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);

  const load = useCallback(async () => {
    try {
      const result = await listRuns({ agentId, status, source, endUser: endUser.trim(), limit: PAGE_SIZE, offset });
      setItems(result.items);
      setTotal(result.total);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load runs");
    }
  }, [agentId, status, source, endUser, offset]);

  useEffect(() => {
    if (detail === null) void load();
  }, [load, detail]);

  async function open(id: string) {
    try {
      setDetail(await getRun(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not open that run");
    }
  }

  if (detail) {
    return (
      <div className="run-detail">
        <button onClick={() => setDetail(null)}>← All runs</button>
        <div className="card">
          <RunTrace run={detail} />
          <ReplayPanel runId={detail.id} liveVersion={null} />
        </div>
      </div>
    );
  }

  return (
    <div>
      <p className="hint">
        Every conversation turn an agent handles is recorded here with each step it took, so you can see why it
        answered or acted the way it did.
      </p>
      <div className="toolbar">
        <select aria-label="Agent" value={agentId} onChange={(e) => { setAgentId(e.target.value); setOffset(0); }}>
          <option value="">All agents</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
        <select aria-label="Status" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
          <option value="">Any status</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s.replace("_", " ")}
            </option>
          ))}
        </select>
        <select aria-label="Source" value={source} onChange={(e) => { setSource(e.target.value); setOffset(0); }}>
          <option value="">API and playground</option>
          <option value="api">API (real customers)</option>
          <option value="playground">Playground (tests)</option>
        </select>
        <input
          placeholder="Customer id"
          value={endUser}
          onChange={(e) => {
            setEndUser(e.target.value);
            setOffset(0);
          }}
        />
        <button onClick={() => void load()}>Refresh</button>
      </div>
      {error && <p className="error">{error}</p>}

      {items === null ? (
        <p>Loading…</p>
      ) : items.length === 0 ? (
        <p className="hint">No runs match. They appear as soon as an agent handles a message.</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>Request</th>
              <th>Agent</th>
              <th>Status</th>
              <th>Actions</th>
              <th>Tokens</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id} tabIndex={0} onClick={() => void open(r.id)} onKeyDown={(e) => { if (e.key === "Enter") void open(r.id); }}>
                <td>
                  <strong>{r.input_preview ?? "—"}</strong>
                  {r.reply_preview && <div className="hint">{r.reply_preview}</div>}
                  <small>
                    {r.end_user_ref} · {r.source}
                  </small>
                </td>
                <td>
                  {r.agent_name}
                  {r.agent_version !== null ? <small> v{r.agent_version}</small> : <small> draft</small>}
                </td>
                <td>
                  <span className={`badge ${statusBadgeClass(r.status)}`}>{r.status.replace("_", " ")}</span>
                  {r.outcome && <small> {r.outcome}</small>}
                </td>
                <td>{r.actions}</td>
                <td>{r.tokens}</td>
                <td>
                  {formatDate(r.started_at)}
                  {r.duration_ms !== null && <small> {r.duration_ms} ms</small>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {total > PAGE_SIZE && (
        <div className="pager">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
            Previous
          </button>
          <span>
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          </span>
          <button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>
            Next
          </button>
        </div>
      )}
    </div>
  );
}
