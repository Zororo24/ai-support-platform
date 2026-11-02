import { useState } from "react";

import type { ActionDef, WfNode, WorkflowDef } from "../types";
import { StepEditor } from "./StepEditor";
import { NODE_LABELS, blankNode, knownVars, outline } from "./workflowModel";

export function WorkflowEditor({
  initial,
  actions,
  existingNames,
  onSave,
  onCancel,
}: {
  initial: WorkflowDef;
  actions: ActionDef[];
  existingNames: string[];
  onSave: (workflow: WorkflowDef) => void;
  onCancel: () => void;
}) {
  const [wf, setWf] = useState<WorkflowDef>(initial);
  const [open, setOpen] = useState<string | null>(initial.start);
  const [newType, setNewType] = useState<WfNode["type"]>("message");
  const [jsonMode, setJsonMode] = useState(false);
  const [jsonText, setJsonText] = useState("");
  const [error, setError] = useState<string | null>(null);

  const ids = Object.keys(wf.nodes);
  const variables = knownVars(wf);
  const confirmIds = ids.filter((id) => wf.nodes[id].type === "confirm");

  function setNode(id: string, node: WfNode) {
    setWf({ ...wf, nodes: { ...wf.nodes, [id]: node } });
  }

  function addStep() {
    let n = ids.length + 1;
    let id = `${newType}_${n}`;
    while (wf.nodes[id]) id = `${newType}_${++n}`;
    setWf({ ...wf, nodes: { ...wf.nodes, [id]: blankNode(newType, ids[0] ?? null) } });
    setOpen(id);
  }

  function renameStep(oldId: string, rawNew: string) {
    const newId = rawNew.trim().toLowerCase().replace(/[^a-z0-9_]/g, "_");
    if (!newId || newId === oldId || wf.nodes[newId] || !/^[a-z]/.test(newId)) return;
    const swap = (v: string | null) => (v === oldId ? newId : v);
    const nodes: Record<string, WfNode> = {};
    for (const [id, node] of Object.entries(wf.nodes)) {
      const copy = JSON.parse(JSON.stringify(node)) as WfNode;
      if ("next" in copy) copy.next = swap(copy.next as string | null) as never;
      if (copy.type === "action") {
        copy.on_error = swap(copy.on_error);
        copy.confirmed_by = swap(copy.confirmed_by);
      }
      if (copy.type === "branch") {
        copy.cases = copy.cases.map((c) => ({ ...c, next: swap(c.next) as string }));
        copy.otherwise = swap(copy.otherwise);
      }
      if (copy.type === "confirm") {
        copy.yes = swap(copy.yes) as string;
        copy.no = swap(copy.no) as string;
      }
      nodes[id === oldId ? newId : id] = copy;
    }
    setWf({ ...wf, nodes, start: wf.start === oldId ? newId : wf.start });
    setOpen(newId);
  }

  function removeStep(id: string) {
    if (ids.length === 1) return setError("A process needs at least one step.");
    if (!window.confirm(`Remove the step “${id}”? Steps that led to it will need a new destination.`)) return;
    const nodes = { ...wf.nodes };
    delete nodes[id];
    const rest = Object.keys(nodes);
    setWf({ ...wf, nodes, start: wf.start === id ? rest[0] : wf.start });
    setOpen(null);
  }

  function save() {
    const name = wf.name.trim();
    if (!/^[a-z][a-z0-9_]{1,39}$/.test(name))
      return setError("The name must be lowercase letters, digits and underscores, like return_request.");
    if (existingNames.includes(name)) return setError(`There is already a process called “${name}”.`);
    if (!wf.description.trim()) return setError("Describe when this process should start. The AI reads this.");
    setError(null);
    onSave({ ...wf, name, description: wf.description.trim() });
  }

  function applyJson() {
    try {
      const parsed = JSON.parse(jsonText) as WorkflowDef;
      setWf(parsed);
      setOpen(parsed.start);
      setJsonMode(false);
      setError(null);
    } catch {
      setError("That isn't valid JSON.");
    }
  }

  const flow = outline(wf);

  return (
    <div className="card workflow-editor">
      <h3>{initial.name ? `Edit process “${initial.name}”` : "New process"}</h3>
      <div className="user-form">
        <label>
          Name
          <input
            value={wf.name}
            onChange={(e) => setWf({ ...wf, name: e.target.value })}
            placeholder="return_request"
            disabled={Boolean(initial.name)}
          />
        </label>
        <label style={{ flex: 1, minWidth: "16rem" }}>
          When should the AI start it?
          <input
            value={wf.description}
            onChange={(e) => setWf({ ...wf, description: e.target.value })}
            placeholder="The customer wants to return an item they ordered."
          />
        </label>
        <label>
          First step
          <select value={wf.start} onChange={(e) => setWf({ ...wf, start: e.target.value })}>
            {ids.map((id) => (
              <option key={id}>{id}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="flow-outline">
        <strong>How it flows</strong>
        <ol>
          {flow.map((row) => (
            <li key={row.id}>
              <button className="linklike" onClick={() => setOpen(row.id)}>
                {row.id}
              </button>
              : {row.text}
              {row.goes.length > 0 && <small className="hint"> {row.goes.join(" · ")}</small>}
            </li>
          ))}
        </ol>
      </div>

      <h4>Steps</h4>
      {ids.map((id) => {
        const node = wf.nodes[id];
        return (
          <details key={id} open={open === id} onToggle={(e) => { if ((e.target as HTMLDetailsElement).open) setOpen(id); }} className="step-card">
            <summary>
              <strong>{id}</strong>
              {wf.start === id && <span className="badge doc-ready">start</span>}
              <span className="hint"> {NODE_LABELS[node.type]}</span>
            </summary>
            <div className="step-body">
              <div className="param-row">
                <label className="inline">
                  Step id
                  <input defaultValue={id} onBlur={(e) => renameStep(id, e.target.value)} />
                </label>
                <button className="danger" onClick={() => removeStep(id)}>
                  Remove step
                </button>
              </div>
              <StepEditor
                node={node}
                onChange={(n) => setNode(id, n)}
                stepIds={ids}
                currentId={id}
                actions={actions}
                variables={variables}
                confirmIds={confirmIds}
              />
            </div>
          </details>
        );
      })}

      <div className="param-row">
        <select value={newType} onChange={(e) => setNewType(e.target.value as WfNode["type"])} aria-label="Step type">
          {(Object.keys(NODE_LABELS) as WfNode["type"][]).map((t) => (
            <option key={t} value={t}>
              {NODE_LABELS[t]}
            </option>
          ))}
        </select>
        <button onClick={addStep}>+ Add step</button>
        <button
          onClick={() => {
            setJsonText(JSON.stringify(wf, null, 2));
            setJsonMode(!jsonMode);
          }}
        >
          {jsonMode ? "Close JSON" : "Edit as JSON"}
        </button>
      </div>
      {jsonMode && (
        <div>
          <textarea rows={14} value={jsonText} onChange={(e) => setJsonText(e.target.value)} style={{ fontFamily: "monospace" }} />
          <button onClick={applyJson}>Apply JSON</button>
        </div>
      )}

      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button className="primary" onClick={save}>
          Keep this process
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
      <p className="hint">
        Changes go into the draft. Use “Save draft” at the top: the platform checks the whole process (broken
        links, unknown values, unreachable steps) and tells you what to fix.
      </p>
    </div>
  );
}
