import { useCallback, useEffect, useState } from "react";

import { getConfigDiff, getVersion, listVersions, makeVersionLive } from "../api/client";
import { formatDate } from "../admin/format";
import type { AgentDetail, AgentVersion, AgentVersionDetail, ConfigDiff } from "../types";
import { DiffView } from "./DiffView";

export function VersionsTab({ agent, onChanged }: { agent: AgentDetail; onChanged: () => Promise<void> }) {
  const [versions, setVersions] = useState<AgentVersion[] | null>(null);
  const [viewing, setViewing] = useState<AgentVersionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [from, setFrom] = useState("live");
  const [to, setTo] = useState("draft");
  const [diff, setDiff] = useState<ConfigDiff | null>(null);

  const load = useCallback(async () => {
    try {
      setVersions(await listVersions(agent.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load versions");
    }
  }, [agent.id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function makeLive(version: number) {
    if (!window.confirm(`Make version ${version} live? Your API will serve it immediately.`)) return;
    setBusy(true);
    setError(null);
    try {
      await makeVersionLive(agent.id, version);
      await load();
      await onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not switch versions");
    } finally {
      setBusy(false);
    }
  }

  async function compare() {
    setError(null);
    try {
      setDiff(await getConfigDiff(agent.id, from, to));
    } catch (e) {
      setDiff(null);
      setError(e instanceof Error ? e.message : "Could not compare those");
    }
  }

  const choices = (
    <>
      <option value="draft">Draft</option>
      <option value="live" disabled={agent.published_version === null}>
        Live version
      </option>
      {versions?.map((v) => (
        <option key={v.version} value={String(v.version)}>
          Version {v.version}
        </option>
      ))}
    </>
  );

  async function view(version: number) {
    try {
      setViewing(await getVersion(agent.id, version));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load that version");
    }
  }

  return (
    <div className="card">
      <h3>Published versions</h3>
      <p className="hint">
        Each publish is saved permanently. Switching back is instant and doesn&apos;t touch your draft.
      </p>
      {error && <p className="error">{error}</p>}
      {versions === null ? (
        <p>Loading…</p>
      ) : versions.length === 0 ? (
        <p className="hint">Nothing published yet.</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>Version</th>
              <th>Note</th>
              <th>Published</th>
              <th>Tests</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version} className="static">
                <td>
                  v{v.version} {v.is_live && <span className="badge doc-ready">Live</span>}
                </td>
                <td>{v.note || <small>—</small>}</td>
                <td>
                  {formatDate(v.created_at)}
                  {v.created_by_email && <small> {v.created_by_email}</small>}
                </td>
                <td>
                  {v.tests === null ? (
                    <small>—</small>
                  ) : v.tests.skipped ? (
                    <span className="badge doc-pending" title={v.tests.override_reason ?? ""}>
                      Skipped
                    </span>
                  ) : v.tests.passed + v.tests.failed === 0 ? (
                    <small>No tests</small>
                  ) : (
                    <span className="badge doc-ready">{v.tests.passed} passed</span>
                  )}
                  {v.tests?.skipped && v.tests.override_reason && <div className="hint">{v.tests.override_reason}</div>}
                </td>
                <td className="row-actions">
                  <button onClick={() => void view(v.version)}>View</button>
                  {!v.is_live && (
                    <button disabled={busy} onClick={() => void makeLive(v.version)}>
                      Roll back to this
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h4>Compare</h4>
      <div className="actions" style={{ alignItems: "center" }}>
        <label className="inline">
          From
          <select value={from} onChange={(e) => setFrom(e.target.value)}>
            {choices}
          </select>
        </label>
        <label className="inline">
          to
          <select value={to} onChange={(e) => setTo(e.target.value)}>
            {choices}
          </select>
        </label>
        <button onClick={() => void compare()}>Show differences</button>
      </div>
      {diff && <DiffView diff={diff} />}
      {viewing && (
        <div style={{ marginTop: "1rem" }}>
          <div className="doc-head">
            <h4 style={{ margin: 0 }}>Version {viewing.version} configuration</h4>
            <button onClick={() => setViewing(null)}>Close</button>
          </div>
          <pre className="code">{JSON.stringify(viewing.config, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
