import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  deleteAgent,
  getAgent,
  getConfigDiff,
  getScenarioRun,
  listScenarioRuns,
  patchAgent,
  publishAgent,
  saveDraft,
} from "../api/client";
import type { AgentConfig, AgentDetail, ConfigDiff, ScenarioRunOut } from "../types";
import { DiffView } from "./DiffView";
import { ActionsTab } from "./ActionsTab";
import { InstructionsTab } from "./InstructionsTab";
import { IntegrationTab } from "./IntegrationTab";
import { KnowledgeTab } from "./KnowledgeTab";
import { OverviewTab } from "./OverviewTab";
import { Results, TestsTab } from "./TestsTab";
import { VersionsTab } from "./VersionsTab";
import { WorkflowsTab } from "./WorkflowsTab";

type Tab =
  | "overview"
  | "instructions"
  | "knowledge"
  | "actions"
  | "workflows"
  | "tests"
  | "integration"
  | "versions";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview & test" },
  { id: "instructions", label: "Instructions & limits" },
  { id: "knowledge", label: "Knowledge" },
  { id: "actions", label: "Actions" },
  { id: "workflows", label: "Processes" },
  { id: "tests", label: "Tests" },
  { id: "integration", label: "Integration" },
  { id: "versions", label: "Versions" },
];

export function AgentDetailPage({ id, onBack }: { id: string; onBack: () => void }) {
  const [agent, setAgent] = useState<AgentDetail | null>(null);
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [note, setNote] = useState("");
  const [versionsKey, setVersionsKey] = useState(0);
  const [changes, setChanges] = useState<ConfigDiff | null>(null);
  const [gate, setGate] = useState<ScenarioRunOut | null>(null); // the tests that blocked a publish
  const [override, setOverride] = useState(false);
  const [reason, setReason] = useState("");

  const load = useCallback(async () => {
    try {
      const loaded = await getAgent(id);
      setAgent(loaded);
      setConfig(loaded.draft_config);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the agent");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error && !agent) {
    return (
      <div>
        <button onClick={onBack}>← Back</button>
        <p className="error">{error}</p>
      </div>
    );
  }
  if (!agent || !config) return <p>Loading…</p>;

  const dirty = JSON.stringify(config) !== JSON.stringify(agent.draft_config);

  async function run(action: () => Promise<void>, success?: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (success) setNotice(success);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  const save = () =>
    run(async () => {
      const saved = await saveDraft(id, config);
      setAgent(saved);
      setConfig(saved.draft_config);
    }, "Draft saved.");

  const publish = () =>
    run(async () => {
      if (dirty) {
        const saved = await saveDraft(id, config);
        setAgent(saved);
      }
      setGate(null);
      try {
        await publishAgent(id, note, { skipTests: override, skipReason: reason });
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          // The tests failed: show which ones, and offer the override.
          const [latest] = await listScenarioRuns(id, 1);
          if (latest?.purpose === "publish_gate") setGate(await getScenarioRun(id, latest.id));
          setOverride(true);
        }
        throw e;
      }
      setNote("");
      setReason("");
      setOverride(false);
      setPublishing(false);
      setChanges(null);
      setVersionsKey((k) => k + 1);
      await load();
    }, "Published. Your API now serves this version.");

  const togglePublishing = () => {
    const opening = !publishing;
    setPublishing(opening);
    setGate(null);
    setOverride(false);
    if (opening) {
      getConfigDiff(id, "live", "draft")
        .then(setChanges)
        .catch(() => setChanges(null));
    }
  };

  const changeNameDescription = (name: string, description: string) =>
    run(async () => {
      const updated = await patchAgent(id, { name, description });
      setAgent(updated);
    }, "Saved.");

  const remove = () => {
    if (!window.confirm(`Delete “${agent.name}”? Its API keys stop working immediately.`)) return;
    void run(async () => {
      await deleteAgent(id);
      onBack();
    });
  };

  return (
    <div className="agent-detail">
      <button onClick={onBack}>← All agents</button>

      <div className="card">
        <div className="doc-head">
          <h2>{agent.name}</h2>
          {agent.published_version === null ? (
            <span className="badge doc-pending">Not published</span>
          ) : (
            <span className="badge doc-ready">Live v{agent.published_version}</span>
          )}
          {(dirty || agent.has_unpublished_changes) && <span className="badge doc-pending">Unpublished changes</span>}
        </div>
        <div className="actions" style={{ marginTop: "0.75rem" }}>
          <button disabled={busy || !dirty} onClick={() => void save()}>
            Save draft
          </button>
          <button className="primary" disabled={busy} onClick={togglePublishing}>
            Publish…
          </button>
          <button className="danger" disabled={busy} onClick={remove}>
            Delete agent
          </button>
        </div>
        {publishing && (
          <div className="publish-box">
            <label>
              What changed? (optional note)
              <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} />
            </label>
            <p className="hint">
              {dirty ? "Your unsaved edits will be saved and published. " : ""}
              Publishing makes this configuration live for everyone calling the API. Your tests run first.
            </p>
            {changes && (
              <details open={!changes.identical}>
                <summary>What changes compared with the live version{dirty ? " (saved draft)" : ""}</summary>
                <DiffView diff={changes} />
              </details>
            )}
            {gate && <Results run={gate} />}
            {override && (
              <label>
                Publish anyway. Why? (recorded in the audit log)
                <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} />
              </label>
            )}
            <button
              className="primary"
              disabled={busy || (override && !reason.trim())}
              onClick={() => void publish()}
            >
              {busy ? "Testing and publishing…" : override ? "Publish without passing tests" : "Publish now"}
            </button>
          </div>
        )}
        {error && <p className="error">{error}</p>}
        {notice && <p className="notice">{notice}</p>}
      </div>

      <div className="subtabs">
        {TABS.map((t) => (
          <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <OverviewTab
          agent={agent}
          dirty={dirty}
          busy={busy}
          onRename={changeNameDescription}
        />
      )}
      {tab === "instructions" && <InstructionsTab config={config} setConfig={setConfig} />}
      {tab === "knowledge" && <KnowledgeTab config={config} setConfig={setConfig} />}
      {tab === "actions" && <ActionsTab agentId={id} config={config} setConfig={setConfig} dirty={dirty} />}
      {tab === "workflows" && <WorkflowsTab agentId={id} config={config} setConfig={setConfig} dirty={dirty} />}
      {tab === "tests" && <TestsTab agentId={id} config={config} dirty={dirty} />}
      {tab === "integration" && <IntegrationTab agent={agent} />}
      {tab === "versions" && <VersionsTab key={versionsKey} agent={agent} onChanged={load} />}
    </div>
  );
}
