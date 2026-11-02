import { useEffect, useState } from "react";

import { listConnections } from "../api/client";
import type { ActionDef, AgentConfig, Connection } from "../types";
import { ActionEditor, blankAction } from "./ActionEditor";
import { ActionTester } from "./ActionTester";
import { HandoffCard } from "./HandoffCard";

type Props = {
  agentId: string;
  config: AgentConfig;
  setConfig: (config: AgentConfig) => void;
  dirty: boolean;
};

export function ActionsTab({ agentId, config, setConfig, dirty }: Props) {
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // index in config.actions being edited, or "new"
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [testing, setTesting] = useState<string | null>(null);

  useEffect(() => {
    listConnections()
      .then(setConnections)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load connections"));
  }, []);

  const connectionName = (a: ActionDef) =>
    a.execution === "handback"
      ? "Your backend"
      : (connections?.find((c) => c.id === a.connection_id)?.name ?? "Unknown connection");

  function save(action: ActionDef) {
    if (editing === "new") {
      setConfig({ ...config, actions: [...config.actions, action] });
    } else if (editing !== null) {
      setConfig({ ...config, actions: config.actions.map((a, i) => (i === editing ? action : a)) });
    }
    setEditing(null);
  }

  function remove(index: number) {
    const target = config.actions[index];
    if (!window.confirm(`Remove the action “${target.name}” from this agent?`)) return;
    setConfig({ ...config, actions: config.actions.filter((_, i) => i !== index) });
    if (testing === target.name) setTesting(null);
  }

  function toggle(index: number) {
    setConfig({
      ...config,
      actions: config.actions.map((a, i) => (i === index ? { ...a, enabled: !a.enabled } : a)),
    });
  }

  if (editing !== null && connections) {
    return (
      <ActionEditor
        key={editing === "new" ? "new" : config.actions[editing].name}
        initial={editing === "new" ? blankAction(connections[0]?.id ?? "") : config.actions[editing]}
        connections={connections}
        onSave={save}
        onCancel={() => setEditing(null)}
      />
    );
  }

  const tested = config.actions.find((a) => a.name === testing) ?? null;

  return (
    <div className="actions-tab">
      <div className="card">
        <div className="doc-head">
          <h3 style={{ margin: 0 }}>Actions</h3>
          <button className="primary" disabled={connections === null} onClick={() => setEditing("new")}>
            Add action
          </button>
        </div>
        <p className="hint">
          Actions are things this agent can do in your systems, like looking up an order or opening a
          ticket. Anything that <strong>changes</strong> something asks the customer to approve first.
        </p>
        {connections !== null && connections.length === 0 && (
          <p className="hint">
            To let us call your API, add a connection first (the <strong>Connections</strong> section). Actions
            your own backend performs don&apos;t need one.
          </p>
        )}
        {error && <p className="error">{error}</p>}

        {config.actions.length === 0 ? (
          <p className="hint">No actions yet. Without any, this agent only answers from your documents.</p>
        ) : (
          <table className="doc-table">
            <thead>
              <tr>
                <th>Action</th>
                <th>Type</th>
                <th>System</th>
                <th>Request</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {config.actions.map((a, i) => (
                <tr key={a.name} className="static" style={{ opacity: a.enabled ? 1 : 0.55 }}>
                  <td>
                    <strong>{a.name}</strong>
                    <div className="hint">{a.description}</div>
                  </td>
                  <td>
                    <span className={`badge ${a.effect === "write" ? "doc-pending" : "doc-ready"}`}>
                      {a.effect === "write" ? "Changes data" : "Reads"}
                    </span>
                    {a.confirmation === "staff" && <div className="hint">Needs staff approval</div>}
                  </td>
                  <td>{connectionName(a)}</td>
                  <td>
                    {a.request ? (
                      <code>
                        {a.request.method} {a.request.path}
                      </code>
                    ) : (
                      <small>Done by your backend</small>
                    )}
                  </td>
                  <td className="row-actions">
                    <button onClick={() => setTesting(a.name)}>Test</button>
                    <button onClick={() => setEditing(i)}>Edit</button>
                    <button onClick={() => toggle(i)}>{a.enabled ? "Disable" : "Enable"}</button>
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

      {tested && <ActionTester key={tested.name} agentId={agentId} action={tested} dirty={dirty} />}

      <HandoffCard config={config} setConfig={setConfig} />
    </div>
  );
}
