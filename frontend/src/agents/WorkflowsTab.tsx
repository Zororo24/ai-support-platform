import { useEffect, useState } from "react";

import { listWorkflowTemplates } from "../api/client";
import type { AgentConfig, WorkflowDef, WorkflowTemplate } from "../types";
import { WorkflowEditor } from "./WorkflowEditor";
import { blankWorkflow } from "./workflowModel";

type Props = {
  agentId: string;
  config: AgentConfig;
  setConfig: (config: AgentConfig) => void;
  dirty: boolean;
};

export function WorkflowsTab({ agentId, config, setConfig, dirty }: Props) {
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // The templates' readiness reflects the *saved* draft, so reload whenever it is saved.
    if (dirty) return;
    listWorkflowTemplates(agentId)
      .then(setTemplates)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load templates"));
  }, [agentId, dirty]);

  const names = config.workflows.map((w) => w.name);

  function save(wf: WorkflowDef) {
    if (editing === "new") setConfig({ ...config, workflows: [...config.workflows, wf] });
    else if (editing !== null) setConfig({ ...config, workflows: config.workflows.map((w, i) => (i === editing ? wf : w)) });
    setEditing(null);
  }

  function addTemplate(t: WorkflowTemplate) {
    if (names.includes(t.workflow.name)) return;
    setConfig({ ...config, workflows: [...config.workflows, t.workflow] });
  }

  function remove(i: number) {
    if (!window.confirm(`Remove the process “${config.workflows[i].name}”?`)) return;
    setConfig({ ...config, workflows: config.workflows.filter((_, j) => j !== i) });
  }

  if (editing !== null) {
    const initial = editing === "new" ? blankWorkflow() : config.workflows[editing];
    return (
      <WorkflowEditor
        key={editing === "new" ? "new" : initial.name}
        initial={initial}
        actions={config.actions}
        existingNames={editing === "new" ? names : names.filter((_, i) => i !== editing)}
        onSave={save}
        onCancel={() => setEditing(null)}
      />
    );
  }

  return (
    <div className="actions-tab">
      <div className="card">
        <div className="doc-head">
          <h3 style={{ margin: 0 }}>Processes</h3>
          <button className="primary" onClick={() => setEditing("new")}>
            New process
          </button>
        </div>
        <p className="hint">
          A process is a guided conversation with a fixed path, like handling a return: it collects what it
          needs, checks your rules, asks the customer to approve, then does the work. It remembers where it is
          if the customer comes back later.
        </p>
        {error && <p className="error">{error}</p>}
        {config.workflows.length === 0 ? (
          <p className="hint">No processes yet. Start from a template below or build your own.</p>
        ) : (
          <table className="doc-table">
            <thead>
              <tr>
                <th>Process</th>
                <th>Steps</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {config.workflows.map((w, i) => (
                <tr key={w.name} className="static" style={{ opacity: w.enabled ? 1 : 0.55 }}>
                  <td>
                    <strong>{w.name}</strong>
                    <div className="hint">{w.description}</div>
                  </td>
                  <td>{Object.keys(w.nodes).length}</td>
                  <td className="row-actions">
                    <button onClick={() => setEditing(i)}>Edit</button>
                    <button
                      onClick={() =>
                        setConfig({ ...config, workflows: config.workflows.map((x, j) => (j === i ? { ...x, enabled: !x.enabled } : x)) })
                      }
                    >
                      {w.enabled ? "Disable" : "Enable"}
                    </button>
                    <button className="danger" onClick={() => remove(i)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Start from a template</h3>
        {dirty && <p className="hint">Save your draft to refresh which templates your actions support.</p>}
        <ul className="check-list">
          {templates.map((t) => (
            <li key={t.id}>
              <div className="doc-head">
                <strong>{t.name}</strong>
                {t.ready ? <span className="badge doc-ready">Ready</span> : <span className="badge doc-pending">Needs setup</span>}
                <button
                  disabled={!t.ready || names.includes(t.workflow.name)}
                  onClick={() => addTemplate(t)}
                  style={{ marginLeft: "auto" }}
                >
                  {names.includes(t.workflow.name) ? "Added" : "Add"}
                </button>
              </div>
              <div className="hint">{t.description}</div>
              {t.missing.length > 0 && <small className="error">Needs: {t.missing.join("; ")}</small>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
