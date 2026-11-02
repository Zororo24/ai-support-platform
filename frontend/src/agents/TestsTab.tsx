import { useCallback, useEffect, useState } from "react";

import {
  createScenario,
  deleteScenario,
  getScenarioRun,
  listScenarioRuns,
  listScenarios,
  listVersions,
  runScenarios,
  updateScenario,
} from "../api/client";
import { formatDate } from "../admin/format";
import type { AgentConfig, AgentVersion, Scenario, ScenarioResult, ScenarioRunOut, ScenarioRunSummary } from "../types";
import { ScenarioEditor } from "./ScenarioEditor";

type Props = { agentId: string; config: AgentConfig; dirty: boolean };

export function TestsTab({ agentId, config, dirty }: Props) {
  const [scenarios, setScenarios] = useState<Scenario[] | null>(null);
  const [versions, setVersions] = useState<AgentVersion[]>([]);
  const [history, setHistory] = useState<ScenarioRunSummary[]>([]);
  const [shown, setShown] = useState<ScenarioRunOut | null>(null);
  const [target, setTarget] = useState("draft");
  const [editing, setEditing] = useState<Scenario | "new" | null>(null);
  const [running, setRunning] = useState<string | null>(null); // scenario id, "all" or null
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [list, past] = await Promise.all([listScenarios(agentId), listScenarioRuns(agentId)]);
      setScenarios(list);
      setHistory(past);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the tests");
    }
  }, [agentId]);

  useEffect(() => {
    void load();
    listVersions(agentId).then(setVersions).catch(() => undefined);
  }, [agentId, load]);

  async function run(ids?: string[]) {
    setRunning(ids?.length === 1 ? ids[0] : "all");
    setError(null);
    try {
      setShown(await runScenarios(agentId, target, ids));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The tests could not run");
    } finally {
      setRunning(null);
    }
  }

  async function open(summary: ScenarioRunSummary) {
    try {
      setShown(await getScenarioRun(agentId, summary.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load that run");
    }
  }

  async function toggle(s: Scenario) {
    try {
      await updateScenario(agentId, s.id, { enabled: !s.enabled });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update the test");
    }
  }

  async function remove(s: Scenario) {
    if (!window.confirm(`Delete the test “${s.name}”?`)) return;
    try {
      await deleteScenario(agentId, s.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not delete the test");
    }
  }

  if (editing !== null) {
    return (
      <ScenarioEditor
        key={editing === "new" ? "new" : editing.id}
        config={config}
        existing={editing === "new" ? null : editing}
        onSave={async (name, definition) => {
          if (editing === "new") await createScenario(agentId, { name, enabled: true, definition });
          else await updateScenario(agentId, editing.id, { name, definition });
          setEditing(null);
          await load();
        }}
        onCancel={() => setEditing(null)}
      />
    );
  }

  const enabledCount = scenarios?.filter((s) => s.enabled).length ?? 0;

  return (
    <div className="tests-tab">
      <div className="card">
        <div className="doc-head">
          <h3 style={{ margin: 0 }}>Tests</h3>
          <button className="primary" onClick={() => setEditing("new")}>
            Add a test
          </button>
        </div>
        <p className="hint">
          A test is a short scripted conversation with checks on what the agent <em>does</em>: which actions it
          uses, what it refuses, how it ends. Tests never touch your real systems. They run before every publish,
          and a failing test stops the publish. Tip: have a conversation on the <strong>Overview &amp; test</strong>{" "}
          tab, then use <strong>Save as a test</strong>.
        </p>
        {dirty && <p className="hint">You have unsaved edits. Tests run against the <em>saved</em> draft.</p>}
        {error && <p className="error">{error}</p>}

        <div className="actions" style={{ alignItems: "center" }}>
          <label className="inline">
            Run against
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="draft">The saved draft</option>
              {versions.map((v) => (
                <option key={v.version} value={String(v.version)}>
                  Version {v.version}
                  {v.is_live ? " (live)" : ""}
                </option>
              ))}
            </select>
          </label>
          <button disabled={running !== null || enabledCount === 0} onClick={() => void run()}>
            {running === "all" ? "Running…" : `Run all (${enabledCount})`}
          </button>
        </div>

        {scenarios === null ? (
          <p>Loading…</p>
        ) : scenarios.length === 0 ? (
          <p className="hint">No tests yet. Without any, publishing is not checked.</p>
        ) : (
          <table className="doc-table">
            <thead>
              <tr>
                <th>Test</th>
                <th>Steps</th>
                <th>Last result</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {scenarios.map((s) => (
                <tr key={s.id} className="static" style={{ opacity: s.enabled ? 1 : 0.55 }}>
                  <td>
                    <strong>{s.name}</strong>
                    {s.definition.description && <div className="hint">{s.definition.description}</div>}
                  </td>
                  <td>{s.definition.steps.length}</td>
                  <td>
                    {s.last_result ? (
                      <>
                        <span className={`badge ${s.last_result.passed ? "doc-ready" : "doc-failed"}`}>
                          {s.last_result.passed ? "Passed" : "Failed"}
                        </span>{" "}
                        <small>
                          on {s.last_result.target} · {formatDate(s.last_result.at)}
                        </small>
                      </>
                    ) : (
                      <small>Not run yet</small>
                    )}
                  </td>
                  <td className="row-actions">
                    <button disabled={running !== null} onClick={() => void run([s.id])}>
                      {running === s.id ? "Running…" : "Run"}
                    </button>
                    <button onClick={() => setEditing(s)}>Edit</button>
                    <button onClick={() => void toggle(s)}>{s.enabled ? "Turn off" : "Turn on"}</button>
                    <button className="danger" onClick={() => void remove(s)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {shown && <Results run={shown} onClose={() => setShown(null)} />}

      {history.length > 0 && (
        <div className="card">
          <h4 style={{ marginTop: 0 }}>Recent runs</h4>
          <table className="doc-table">
            <tbody>
              {history.map((h) => (
                <tr key={h.id} className="static">
                  <td>{formatDate(h.created_at)}</td>
                  <td>{h.purpose === "publish_gate" ? "Before publishing" : "Run by hand"}</td>
                  <td>{h.target}</td>
                  <td>
                    <span className={`badge ${h.failed ? "doc-failed" : "doc-ready"}`}>
                      {h.failed ? `${h.failed} failed` : "All passed"}
                    </span>{" "}
                    <small>{h.passed + h.failed} tests</small>
                  </td>
                  <td className="row-actions">
                    <button onClick={() => void open(h)}>Details</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function Results({ run, onClose }: { run: ScenarioRunOut; onClose?: () => void }) {
  return (
    <div className="card">
      <div className="doc-head">
        <h4 style={{ margin: 0 }}>
          {run.purpose === "publish_gate" ? "Tests before publishing" : "Test results"} · {run.target} ·{" "}
          <span className={run.failed ? "error" : ""}>
            {run.failed ? `${run.failed} of ${run.passed + run.failed} failed` : `${run.passed} passed`}
          </span>
        </h4>
        {onClose && <button onClick={onClose}>Close</button>}
      </div>
      {run.results.length === 0 && <p className="hint">There were no tests turned on.</p>}
      {run.results.map((r) => (
        <ResultRow key={r.scenario_id} result={r} />
      ))}
    </div>
  );
}

function ResultRow({ result }: { result: ScenarioResult }) {
  return (
    <details open={!result.passed} style={{ marginTop: "0.5rem" }}>
      <summary>
        <span className={`badge ${result.passed ? "doc-ready" : "doc-failed"}`}>{result.passed ? "Passed" : "Failed"}</span>{" "}
        <strong>{result.name}</strong> <small>{result.duration_ms} ms</small>
      </summary>
      {result.error && <p className="error">{result.error}</p>}
      <ol className="test-steps">
        {result.steps.map((s) => (
          <li key={s.index}>
            <div>
              <small>{s.say !== null ? "Customer says" : "Customer presses"}</small>{" "}
              <strong>{s.say ?? (s.confirm === "yes" ? "Yes" : "No")}</strong>
            </div>
            {s.reply !== undefined && (
              <div className="hint">
                Agent ({s.status}
                {s.actions_called && s.actions_called.length > 0
                  ? `; used ${s.actions_called.map((a) => a.name + (a.ok ? "" : " (failed)")).join(", ")}`
                  : ""}
                ): {s.reply}
              </div>
            )}
            {s.failures.map((f, i) => (
              <p className="error" key={i} style={{ margin: "0.2rem 0" }}>
                ✗ {f}
              </p>
            ))}
            {s.error && <p className="error">{s.error}</p>}
          </li>
        ))}
      </ol>
    </details>
  );
}
