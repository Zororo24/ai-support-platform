import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { bulkTicketStatus, createTicket, listTickets } from "../api/client";
import { formatDate } from "../admin/format";
import type { Role, Ticket, TicketPriority, TicketStatus } from "../types";
import { TicketDetailView } from "./TicketDetailView";

const PAGE_SIZE = 25;

function NewTicketForm({ onCreated, onCancel }: { onCreated: (t: Ticket) => void; onCancel: () => void }) {
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [priority, setPriority] = useState<TicketPriority>("normal");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(await createTicket(subject.trim(), body.trim(), priority));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the ticket");
      setBusy(false);
    }
  }

  return (
    <form className="card new-ticket" onSubmit={onSubmit}>
      <h3>New ticket</h3>
      <label>
        Subject
        <input value={subject} onChange={(e) => setSubject(e.target.value)} maxLength={300} required />
      </label>
      <label>
        Describe the problem
        <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={5} required maxLength={10000} />
      </label>
      <label>
        Priority
        <select value={priority} onChange={(e) => setPriority(e.target.value as TicketPriority)}>
          <option value="low">Low</option>
          <option value="normal">Normal</option>
          <option value="high">High</option>
        </select>
      </label>
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy || !subject.trim() || !body.trim()}>
          {busy ? "Creating…" : "Create ticket"}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function TicketsPage({ role }: { role: Role }) {
  const isAdmin = role === "admin";
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const [items, setItems] = useState<Ticket[] | null>(null);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState<TicketStatus | "">("");
  const [priority, setPriority] = useState<TicketPriority | "">("");
  const [assignee, setAssignee] = useState("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery(search.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const load = useCallback(async () => {
    try {
      const result = await listTickets({ status, priority, assignee, q: query, limit: PAGE_SIZE, offset });
      setItems(result.items);
      setTotal(result.total);
      setSelected(new Set());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load tickets");
    }
  }, [status, priority, assignee, query, offset]);

  useEffect(() => {
    if (openId === null) void load();
  }, [load, openId]);

  if (openId !== null) {
    return <TicketDetailView id={openId} role={role} onBack={() => setOpenId(null)} />;
  }

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function closeSelected() {
    if (selected.size === 0) return;
    if (!window.confirm(`Close ${selected.size} ticket${selected.size === 1 ? "" : "s"}?`)) return;
    setBusy(true);
    try {
      await bulkTicketStatus([...selected], "closed");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Bulk update failed");
    } finally {
      setBusy(false);
    }
  }

  const allSelected = items !== null && items.length > 0 && items.every((t) => selected.has(t.id));

  return (
    <div className="tickets-page">
      <div className="toolbar">
        <input
          type="search"
          placeholder="Search subject or description"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          aria-label="Status"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value as TicketStatus | "");
            setOffset(0);
          }}
        >
          <option value="">All statuses</option>
          <option value="open">Open</option>
          <option value="pending">Pending</option>
          <option value="closed">Closed</option>
        </select>
        <select
          aria-label="Priority"
          value={priority}
          onChange={(e) => {
            setPriority(e.target.value as TicketPriority | "");
            setOffset(0);
          }}
        >
          <option value="">Any priority</option>
          <option value="high">High</option>
          <option value="normal">Normal</option>
          <option value="low">Low</option>
        </select>
        {isAdmin && (
          <select
            aria-label="Assignee"
            value={assignee}
            onChange={(e) => {
              setAssignee(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">Anyone</option>
            <option value="me">Assigned to me</option>
            <option value="unassigned">Unassigned</option>
          </select>
        )}
        <button className="primary" onClick={() => setCreating((v) => !v)}>
          {creating ? "Close" : "New ticket"}
        </button>
      </div>

      {creating && (
        <NewTicketForm
          onCancel={() => setCreating(false)}
          onCreated={(ticket) => {
            setCreating(false);
            setOpenId(ticket.id);
          }}
        />
      )}

      {isAdmin && selected.size > 0 && (
        <div className="bulk-bar">
          {selected.size} selected
          <button disabled={busy} onClick={() => void closeSelected()}>
            Close selected
          </button>
        </div>
      )}

      {error && <p className="error">{error}</p>}

      {items === null ? (
        <p>Loading…</p>
      ) : items.length === 0 ? (
        <p className="hint">
          {query || status || priority || assignee ? "No tickets match these filters." : "No tickets yet."}
        </p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              {isAdmin && (
                <th>
                  <input
                    type="checkbox"
                    aria-label="Select all"
                    checked={allSelected}
                    onChange={() => setSelected(allSelected ? new Set() : new Set(items.map((t) => t.id)))}
                  />
                </th>
              )}
              <th>Subject</th>
              <th>Status</th>
              <th>Priority</th>
              {isAdmin && <th>From</th>}
              {isAdmin && <th>Assignee</th>}
              <th>Opened</th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr
                key={t.id}
                tabIndex={0}
                onClick={() => setOpenId(t.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") setOpenId(t.id);
                }}
              >
                {isAdmin && (
                  <td onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      aria-label={`Select ${t.subject}`}
                      checked={selected.has(t.id)}
                      onChange={() => toggle(t.id)}
                    />
                  </td>
                )}
                <td>
                  <strong>{t.subject}</strong>
                  <small> #{t.id.slice(0, 8)}</small>
                </td>
                <td>
                  <span className={`badge ticket-${t.status}`}>{t.status}</span>
                </td>
                <td>
                  <span className={`badge prio-${t.priority}`}>{t.priority}</span>
                </td>
                {isAdmin && <td>{t.requester_email}</td>}
                {isAdmin && <td>{t.assignee_email ?? <small>Unassigned</small>}</td>}
                <td>{t.created_at ? formatDate(t.created_at) : ""}</td>
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
