import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { addTicketComment, getTicket, listUsers, patchTicket } from "../api/client";
import { formatDate } from "../admin/format";
import type { AdminUser, Role, TicketDetail, TicketPriority, TicketStatus } from "../types";

type Props = {
  id: string;
  role: Role;
  onBack: () => void;
};

export function TicketDetailView({ id, role, onBack }: Props) {
  const isAdmin = role === "admin";
  const [ticket, setTicket] = useState<TicketDetail | null>(null);
  const [admins, setAdmins] = useState<AdminUser[]>([]);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setTicket(await getTicket(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the ticket");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!isAdmin) return;
    listUsers()
      .then((users) => setAdmins(users.filter((u) => u.role === "admin" && u.is_active)))
      .catch(() => setAdmins([]));
  }, [isAdmin]);

  async function triage(changes: {
    status?: TicketStatus;
    priority?: TicketPriority;
    assignee_id?: string | null;
  }) {
    setBusy(true);
    setError(null);
    try {
      await patchTicket(id, changes);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Update failed");
    } finally {
      setBusy(false);
    }
  }

  async function onReply(event: FormEvent) {
    event.preventDefault();
    if (!reply.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await addTicketComment(id, reply.trim());
      setReply("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not post your reply");
    } finally {
      setBusy(false);
    }
  }

  if (error && !ticket) {
    return (
      <div>
        <button onClick={onBack}>← Back</button>
        <p className="error">{error}</p>
      </div>
    );
  }
  if (!ticket) return <p>Loading…</p>;

  return (
    <div className="ticket-detail">
      <button onClick={onBack}>← Back to tickets</button>

      <div className="card">
        <div className="doc-head">
          <h2>{ticket.subject}</h2>
          <span className={`badge ticket-${ticket.status}`}>{ticket.status}</span>
          <span className={`badge prio-${ticket.priority}`}>{ticket.priority}</span>
        </div>
        <dl className="meta">
          <dt>Ticket</dt>
          <dd>#{ticket.id.slice(0, 8)}</dd>
          {ticket.requester_email && (
            <>
              <dt>From</dt>
              <dd>{ticket.requester_email}</dd>
            </>
          )}
          <dt>Assigned to</dt>
          <dd>{ticket.assignee_email ?? "Unassigned"}</dd>
          {ticket.created_at && (
            <>
              <dt>Opened</dt>
              <dd>
                {formatDate(ticket.created_at)}
                {ticket.conversation_id ? " · created from a chat conversation" : ""}
              </dd>
            </>
          )}
        </dl>
        <p className="ticket-body">{ticket.body}</p>

        {isAdmin && (
          <div className="triage">
            <label>
              Status
              <select
                value={ticket.status}
                disabled={busy}
                onChange={(e) => void triage({ status: e.target.value as TicketStatus })}
              >
                <option value="open">Open</option>
                <option value="pending">Pending (waiting on customer)</option>
                <option value="closed">Closed</option>
              </select>
            </label>
            <label>
              Priority
              <select
                value={ticket.priority}
                disabled={busy}
                onChange={(e) => void triage({ priority: e.target.value as TicketPriority })}
              >
                <option value="low">Low</option>
                <option value="normal">Normal</option>
                <option value="high">High</option>
              </select>
            </label>
            <label>
              Assignee
              <select
                value={ticket.assignee_id ?? ""}
                disabled={busy}
                onChange={(e) => void triage({ assignee_id: e.target.value || null })}
              >
                <option value="">Unassigned</option>
                {admins.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.email}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        {error && <p className="error">{error}</p>}
      </div>

      <div className="card">
        <h3>Conversation ({ticket.comments.length})</h3>
        {ticket.comments.length === 0 && <p className="hint">No replies yet.</p>}
        <ul className="thread">
          {ticket.comments.map((c) => (
            <li key={c.id} className={c.author_role === "admin" ? "staff" : "customer"}>
              <div className="who-line">
                <strong>{c.author_email}</strong>
                {c.author_role === "admin" && <span className="badge">Support</span>}
                <small>{formatDate(c.created_at)}</small>
              </div>
              <p>{c.body}</p>
            </li>
          ))}
        </ul>
        <form onSubmit={onReply} className="reply-form">
          <textarea
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            rows={3}
            maxLength={5000}
            placeholder={isAdmin ? "Reply to the customer…" : "Add a reply…"}
          />
          <button type="submit" className="primary" disabled={busy || !reply.trim()}>
            {busy ? "Sending…" : "Send reply"}
          </button>
        </form>
        {!isAdmin && ticket.status === "closed" && (
          <p className="hint">This ticket is closed. Replying will reopen it.</p>
        )}
      </div>
    </div>
  );
}
