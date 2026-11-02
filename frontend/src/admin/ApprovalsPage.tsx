import { useCallback, useEffect, useState } from "react";

import { decideApproval, listApprovals } from "../api/client";
import type { Approval } from "../types";
import { formatDate } from "./format";

type Filter = "pending" | "decided";

export function ApprovalsPage() {
  const [filter, setFilter] = useState<Filter>("pending");
  const [items, setItems] = useState<Approval[] | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await listApprovals(filter === "pending" ? "pending" : undefined);
      setItems(filter === "pending" ? result.items : result.items.filter((a) => a.status !== "pending"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load approvals");
    }
  }, [filter]);

  useEffect(() => {
    setItems(null);
    void load();
  }, [load]);

  async function decide(a: Approval, decision: "approve" | "deny") {
    setBusy(a.id);
    setMessage(null);
    setError(null);
    try {
      const result = await decideApproval(a.id, decision, notes[a.id] ?? "");
      setMessage(
        decision === "approve"
          ? `Approved. The customer was told: “${result.reply}”`
          : "Declined. The customer was told their request was declined.",
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not record the decision");
      await load();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div className="toolbar">
        <p className="hint" style={{ flex: 1, margin: 0 }}>
          Some actions need a person on your team to allow them, for example a large refund. The customer is
          told their request is with your team. Approving runs it exactly as shown here; declining runs nothing.
        </p>
        <button className={filter === "pending" ? "primary" : ""} onClick={() => setFilter("pending")}>
          Waiting
        </button>
        <button className={filter === "decided" ? "primary" : ""} onClick={() => setFilter("decided")}>
          History
        </button>
      </div>
      {message && <p className="hint">{message}</p>}
      {error && <p className="error">{error}</p>}

      {items === null ? (
        <p>Loading…</p>
      ) : items.length === 0 ? (
        <p className="hint">{filter === "pending" ? "Nothing is waiting for approval." : "No decisions yet."}</p>
      ) : (
        items.map((a) => (
          <div className="card" key={a.id}>
            <div className="doc-head">
              <div>
                <strong>{a.summary}</strong>
                <div className="hint">
                  {a.agent_name} · customer <code>{a.end_user_ref}</code> · asked {formatDate(a.requested_at)}
                  {a.status === "pending" && <> · expires {formatDate(a.expires_at)}</>}
                </div>
              </div>
              {a.status !== "pending" && (
                <span className={`badge ${a.status === "approved" ? "doc-ready" : a.status === "denied" ? "doc-failed" : "doc-pending"}`}>
                  {a.status}
                </span>
              )}
            </div>
            <p className="hint" style={{ marginBottom: 0 }}>
              Action <code>{a.action}</code> with
            </p>
            <pre className="code">{JSON.stringify(a.args, null, 2)}</pre>
            {a.status === "pending" ? (
              <div className="user-form">
                <label style={{ flex: 1, minWidth: "14rem" }}>
                  Note for the customer (optional)
                  <input
                    value={notes[a.id] ?? ""}
                    maxLength={300}
                    onChange={(e) => setNotes({ ...notes, [a.id]: e.target.value })}
                    placeholder="Added to the reply if you decline"
                  />
                </label>
                <button className="primary" disabled={busy === a.id} onClick={() => void decide(a, "approve")}>
                  Approve and run
                </button>
                <button className="danger" disabled={busy === a.id} onClick={() => void decide(a, "deny")}>
                  Decline
                </button>
              </div>
            ) : (
              <p className="hint">
                {a.decided_by ? `By ${a.decided_by}` : "No decision was made in time"}
                {a.decided_at && <> · {formatDate(a.decided_at)}</>}
                {a.note && <> · “{a.note}”</>}
              </p>
            )}
          </div>
        ))
      )}
    </div>
  );
}
