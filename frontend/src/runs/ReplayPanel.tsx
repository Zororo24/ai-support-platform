import { useState } from "react";

import { replayRun } from "../api/client";
import type { ReplayResult } from "../types";

/** Re-run a past conversation against the draft (or another version) and see what changes. */
export function ReplayPanel({ runId, liveVersion }: { runId: string; liveVersion: number | null }) {
  const [target, setTarget] = useState("draft");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ReplayResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      setResult(await replayRun(runId, target));
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : "The replay failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Replay this conversation</h3>
      <p className="hint">
        Plays the customer&apos;s messages again against another configuration. Actions answer with what they
        returned the first time, so nothing is sent to your systems.
      </p>
      <div className="actions" style={{ alignItems: "center" }}>
        <label className="inline">
          Against
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="draft">The saved draft</option>
            {liveVersion !== null && <option value={String(liveVersion)}>Live version (v{liveVersion})</option>}
          </select>
        </label>
        <button disabled={busy} onClick={() => void run()}>
          {busy ? "Replaying…" : "Replay"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {result && (
        <>
          <p className={result.differs ? "error" : "hint"}>
            {result.differs ? "The behaviour differs from what happened." : "Same behaviour as before."}
            {result.truncated && " Only the last 12 messages were replayed."}
          </p>
          {result.turns.map((t, i) => (
            <div key={i} className="card" style={{ background: "#fafbfc" }}>
              <strong>{t.say}</strong>
              <div className="user-form">
                <div style={{ flex: 1, minWidth: "14rem" }}>
                  <small className="hint">Then ({t.original.status ?? "?"})</small>
                  <p style={{ margin: "0.2rem 0" }}>{t.original.reply}</p>
                  <small>Actions: {t.original.actions.join(", ") || "none"}</small>
                </div>
                <div style={{ flex: 1, minWidth: "14rem" }}>
                  <small className="hint">Now ({t.replayed?.status ?? "failed"})</small>
                  {t.error ? (
                    <p className="error">{t.error}</p>
                  ) : (
                    <>
                      <p style={{ margin: "0.2rem 0" }}>{t.replayed?.reply}</p>
                      <small>Actions: {t.replayed?.actions.join(", ") || "none"}</small>
                    </>
                  )}
                </div>
              </div>
              {t.differs && <span className="badge doc-failed">differs</span>}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
