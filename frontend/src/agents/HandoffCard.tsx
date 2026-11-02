import type { AgentConfig } from "../types";

type Props = {
  config: AgentConfig;
  setConfig: (config: AgentConfig) => void;
};

const SUGGESTIONS = [
  { value: "{{handoff.reason}}", label: "why it was handed off" },
  { value: "{{handoff.summary}}", label: "a summary of the conversation" },
  { value: "{{context.customer_id}}", label: "the customer's id (from your system)" },
];

/** Optional automation for the moment the agent passes a customer to a person. */
export function HandoffCard({ config, setConfig }: Props) {
  const handoff = config.handoff;
  const action = handoff ? config.actions.find((a) => a.name === handoff.action) : undefined;

  function choose(name: string) {
    if (!name) return setConfig({ ...config, handoff: null });
    const chosen = config.actions.find((a) => a.name === name);
    const args: Record<string, string> = {};
    for (const [param, def] of Object.entries(chosen?.parameters ?? {})) {
      if (!def.required) continue;
      args[param] = /summary|detail|body|description|note/i.test(param) ? "{{handoff.summary}}" : "{{handoff.reason}}";
    }
    setConfig({ ...config, handoff: { action: name, args } });
  }

  function setArg(param: string, value: string) {
    if (!handoff) return;
    const args = { ...handoff.args };
    if (value.trim()) args[param] = value;
    else delete args[param];
    setConfig({ ...config, handoff: { ...handoff, args } });
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>When the agent hands over to a person</h3>
      <p className="hint">
        The agent can pass a customer to your team. You can also have it do something at that moment, like
        opening a ticket in your help desk, so nothing depends on your chat screen noticing. It runs
        straight away without asking the customer. Your webhooks (see <strong>Webhooks</strong>) also receive
        a <code>handoff.requested</code> event.
      </p>
      <label>
        Run this action
        <select value={handoff?.action ?? ""} onChange={(e) => choose(e.target.value)}>
          <option value="">Nothing, just report the handoff</option>
          {config.actions
            .filter((a) => a.enabled && a.effect === "write")
            .map((a) => (
              <option key={a.name} value={a.name}>
                {a.name}
              </option>
            ))}
        </select>
      </label>
      {handoff && !action && (
        <p className="error">The action “{handoff.action}” is no longer on this agent. Pick another one.</p>
      )}
      {handoff && action && (
        <>
          <h4>Details to send</h4>
          {Object.entries(action.parameters).map(([param, def]) => (
            <div className="param-row" key={param}>
              <strong style={{ minWidth: "9rem" }}>
                {param}
                {def.required ? " *" : ""}
              </strong>
              <input
                aria-label={param}
                value={handoff.args[param] ?? ""}
                onChange={(e) => setArg(param, e.target.value)}
                placeholder="{{handoff.reason}}"
                list={`handoff-${param}`}
              />
              <datalist id={`handoff-${param}`}>
                {SUGGESTIONS.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </datalist>
            </div>
          ))}
          <p className="hint">
            Use <code>{"{{handoff.reason}}"}</code>, <code>{"{{handoff.summary}}"}</code> or{" "}
            <code>{"{{context.…}}"}</code>. You can mix them with your own text.
          </p>
        </>
      )}
    </div>
  );
}
