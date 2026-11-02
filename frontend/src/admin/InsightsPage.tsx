import { useEffect, useState } from "react";

import { getAuditLog, getInsightsSummary, getKnowledgeGaps, getUsage, listAgents } from "../api/client";
import type { AgentSummary, AuditList, InsightsSummary, KnowledgeGap, UsageInfo } from "../types";
import { formatDate } from "./format";

type Section = "overview" | "gaps" | "usage" | "audit";

const OUTCOME_LABELS: Record<string, string> = {
  answered: "Answered from documents",
  refused: "Couldn't find an answer",
  order_found: "Order looked up",
  order_not_found: "Order not found",
  order_needs_number: "Asked for order number",
  ticket_created: "Ticket created",
  clarify: "Asked to clarify",
  other: "Other / greeting",
};

function percent(value: number | null): string {
  return value === null ? "–" : `${Math.round(value * 100)}%`;
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="tile card">
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
      {hint && <small>{hint}</small>}
    </div>
  );
}

function GapsTable({ gaps, onAddDocument }: { gaps: KnowledgeGap[]; onAddDocument: (q: string) => void }) {
  if (gaps.length === 0) {
    return <p className="hint">No gaps in this period. Every question was answered and none were rated down.</p>;
  }
  return (
    <table className="doc-table">
      <thead>
        <tr>
          <th>Question</th>
          <th>Asked</th>
          <th>No answer</th>
          <th>👎</th>
          <th>Last seen</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {gaps.map((g) => (
          <tr key={g.question} className="static">
            <td>{g.question}</td>
            <td>{g.count}</td>
            <td>{g.refused}</td>
            <td>{g.thumbs_down}</td>
            <td>{formatDate(g.last_seen)}</td>
            <td>
              <button onClick={() => onAddDocument(g.question)}>Add a document</button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Overview({ days, agentId, onAddDocument }: { days: number; agentId: string; onAddDocument: (q: string) => void }) {
  const [data, setData] = useState<InsightsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    getInsightsSummary(days, agentId || undefined)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load insights"));
  }, [days, agentId]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p>Loading…</p>;
  if (data.replies === 0) {
    return <p className="hint">No conversations in the last {days} days yet. Activity will appear here.</p>;
  }

  const peak = Math.max(1, ...data.series.map((p) => p.messages));
  const outcomes = Object.entries(data.outcomes).sort((a, b) => b[1] - a[1]);

  return (
    <div className="insights">
      <div className="tiles">
        <Tile label="Conversations" value={String(data.conversations)} hint={`${data.replies} replies`} />
        <Tile
          label="Answered from documents"
          value={percent(data.answered_rate)}
          hint="of knowledge questions"
        />
        <Tile label="Tickets from chat" value={String(data.tickets_from_chat)} />
        <Tile
          label="Rated helpful"
          value={percent(data.helpful_rate)}
          hint={`${data.thumbs_up} 👍 · ${data.thumbs_down} 👎`}
        />
        <Tile
          label="Avg response time"
          value={data.avg_latency_ms === null ? "–" : `${(data.avg_latency_ms / 1000).toFixed(1)}s`}
          hint={`${data.total_tokens.toLocaleString()} tokens`}
        />
      </div>

      <div className="card">
        <h3>Replies per day</h3>
        <div className="bars" role="img" aria-label="Replies per day">
          {data.series.map((p) => (
            <div key={p.date} className="bar-col" title={`${p.date}: ${p.messages} replies, ${p.conversations} conversations`}>
              <div className="bar" style={{ height: `${(p.messages / peak) * 100}%` }} />
            </div>
          ))}
        </div>
        <div className="bar-axis">
          <small>{data.series[0].date}</small>
          <small>{data.series[data.series.length - 1].date}</small>
        </div>
      </div>

      <div className="card">
        <h3>What happened to each question</h3>
        <ul className="outcomes">
          {outcomes.map(([key, count]) => (
            <li key={key}>
              <span>{OUTCOME_LABELS[key] ?? key}</span>
              <span className="outcome-bar">
                <span style={{ width: `${(count / data.replies) * 100}%` }} />
              </span>
              <strong>{count}</strong>
            </li>
          ))}
        </ul>
      </div>

      <div className="card">
        <h3>Top unanswered questions</h3>
        <GapsTable gaps={data.top_gaps} onAddDocument={onAddDocument} />
      </div>
    </div>
  );
}

function Gaps({ days, agentId, onAddDocument }: { days: number; agentId: string; onAddDocument: (q: string) => void }) {
  const [gaps, setGaps] = useState<KnowledgeGap[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setGaps(null);
    getKnowledgeGaps(days, agentId || undefined)
      .then(setGaps)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load gaps"));
  }, [days, agentId]);

  if (error) return <p className="error">{error}</p>;
  if (!gaps) return <p>Loading…</p>;
  return (
    <>
      <p className="hint">
        Questions the assistant couldn&apos;t answer from your documents, or that customers rated 👎.
        Adding a document that covers them is the fastest way to improve answers.
      </p>
      <GapsTable gaps={gaps} onAddDocument={onAddDocument} />
    </>
  );
}

function Usage({ days }: { days: number }) {
  const [data, setData] = useState<UsageInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    getUsage(days)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load usage"));
  }, [days]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p>Loading…</p>;
  if (data.totals.runs === 0) return <p className="hint">No agent activity in the last {days} days.</p>;

  const peak = Math.max(1, ...data.series.map((p) => p.runs));
  return (
    <div className="insights">
      <div className="tiles">
        <Tile label="Conversation turns" value={data.totals.runs.toLocaleString()} />
        <Tile label="Tokens used" value={data.totals.tokens.toLocaleString()} hint="model usage" />
        <Tile label="Actions run" value={data.totals.action_calls.toLocaleString()} />
        <Tile label="Failed turns" value={data.totals.failed.toLocaleString()} hint="see the Runs section" />
      </div>
      <div className="card">
        <h3>Turns per day</h3>
        <div className="bars" role="img" aria-label="Turns per day">
          {data.series.map((p) => (
            <div key={p.date} className="bar-col" title={`${p.date}: ${p.runs} turns, ${p.tokens} tokens, ${p.action_calls} actions`}>
              <div className="bar" style={{ height: `${(p.runs / peak) * 100}%` }} />
            </div>
          ))}
        </div>
        <div className="bar-axis">
          <small>{data.series[0].date}</small>
          <small>{data.series[data.series.length - 1].date}</small>
        </div>
      </div>
      <div className="card">
        <h3>By agent</h3>
        <table className="doc-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Turns</th>
              <th>Tokens</th>
              <th>Actions</th>
              <th>Failed</th>
              <th>Tokens today</th>
            </tr>
          </thead>
          <tbody>
            {data.by_agent.map((a) => (
              <tr key={a.agent_id} className="static">
                <td>{a.name}</td>
                <td>{a.runs}</td>
                <td>{a.tokens.toLocaleString()}</td>
                <td>{a.action_calls}</td>
                <td>{a.failed}</td>
                <td>
                  {a.daily_budget ? (
                    <span className={a.tokens_today >= a.daily_budget ? "error" : ""}>
                      {a.tokens_today.toLocaleString()} / {a.daily_budget.toLocaleString()}
                    </span>
                  ) : (
                    <small>{a.tokens_today.toLocaleString()} (no limit)</small>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const FILTERS = [
  { value: "", label: "All activity" },
  { value: "document.", label: "Documents" },
  { value: "user.", label: "Users" },
  { value: "ticket.", label: "Tickets" },
];
const PAGE_SIZE = 25;

function describe(detail: Record<string, unknown> | null): string {
  if (!detail) return "";
  return Object.entries(detail)
    .map(([key, value]) => {
      if (value && typeof value === "object" && "from" in value && "to" in value) {
        const change = value as { from: unknown; to: unknown };
        return `${key}: ${String(change.from)} → ${String(change.to)}`;
      }
      return `${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`;
    })
    .join(" · ");
}

function Audit() {
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<AuditList | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getAuditLog({ action: filter, limit: PAGE_SIZE, offset })
      .then((result) => {
        setData(result);
        setError(null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load the audit log"));
  }, [filter, offset]);

  return (
    <div>
      <div className="toolbar">
        <select
          aria-label="Filter activity"
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value);
            setOffset(0);
          }}
        >
          {FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
      </div>
      {error && <p className="error">{error}</p>}
      {!data ? (
        <p>Loading…</p>
      ) : data.items.length === 0 ? (
        <p className="hint">Nothing recorded yet.</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Who</th>
              <th>Action</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((e) => (
              <tr key={e.id} className="static">
                <td>{formatDate(e.created_at)}</td>
                <td>{e.actor_email}</td>
                <td>
                  <code>{e.action}</code>
                </td>
                <td>{describe(e.detail)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data && data.total > PAGE_SIZE && (
        <div className="pager">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
            Previous
          </button>
          <span>
            {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} of {data.total}
          </span>
          <button disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>
            Next
          </button>
        </div>
      )}
    </div>
  );
}

export function InsightsPage({ onAddDocument }: { onAddDocument: (question: string) => void }) {
  const [section, setSection] = useState<Section>("overview");
  const [days, setDays] = useState(30);
  const [agentId, setAgentId] = useState("");
  const [agents, setAgents] = useState<AgentSummary[]>([]);

  useEffect(() => {
    listAgents()
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);

  return (
    <div>
      <div className="subtabs">
        <button className={section === "overview" ? "active" : ""} onClick={() => setSection("overview")}>
          Overview
        </button>
        <button className={section === "gaps" ? "active" : ""} onClick={() => setSection("gaps")}>
          Knowledge gaps
        </button>
        <button className={section === "usage" ? "active" : ""} onClick={() => setSection("usage")}>
          Usage
        </button>
        <button className={section === "audit" ? "active" : ""} onClick={() => setSection("audit")}>
          Audit log
        </button>
        {(section === "overview" || section === "gaps") && agents.length > 1 && (
          <select aria-label="Agent" value={agentId} onChange={(e) => setAgentId(e.target.value)} style={{ marginLeft: "auto" }}>
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        )}
        {section !== "audit" && (
          <select
            aria-label="Time range"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            style={{ marginLeft: agents.length > 1 && section !== "usage" ? undefined : "auto" }}
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
        )}
      </div>
      {section === "overview" && <Overview days={days} agentId={agentId} onAddDocument={onAddDocument} />}
      {section === "gaps" && <Gaps days={days} agentId={agentId} onAddDocument={onAddDocument} />}
      {section === "usage" && <Usage days={days} />}
      {section === "audit" && <Audit />}
    </div>
  );
}
