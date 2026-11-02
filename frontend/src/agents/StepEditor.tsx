import type { ActionDef, WfField, WfNode } from "../types";
import { ConditionBuilder } from "./ConditionBuilder";
import { blankField } from "./workflowModel";

type Props = {
  node: WfNode;
  onChange: (node: WfNode) => void;
  stepIds: string[];
  currentId: string;
  actions: ActionDef[];
  variables: string[];
  confirmIds: string[];
};

function TargetSelect({
  value,
  onChange,
  stepIds,
  allowEnd,
  label,
}: {
  value: string | null;
  onChange: (v: string | null) => void;
  stepIds: string[];
  allowEnd: boolean;
  label: string;
}) {
  return (
    <label>
      {label}
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
        {allowEnd ? <option value="">(finish here)</option> : <option value="">Choose a step…</option>}
        {stepIds.map((id) => (
          <option key={id} value={id}>
            {id}
          </option>
        ))}
      </select>
    </label>
  );
}

export function StepEditor({ node, onChange, stepIds, currentId, actions, variables, confirmIds }: Props) {
  const others = stepIds.filter((id) => id !== currentId);
  const hint = (
    <p className="hint">
      You can use earlier values in text as <code>{"{{vars.name}}"}</code>
      {variables.length > 0 ? ` (available: ${variables.map((v) => `vars.${v}`).join(", ")})` : ""} and customer
      details as <code>{"{{context.customer_id}}"}</code>.
    </p>
  );

  if (node.type === "collect") {
    const setField = (i: number, change: Partial<WfField>) =>
      onChange({ ...node, fields: node.fields.map((f, j) => (j === i ? { ...f, ...change } : f)) });
    return (
      <div>
        {node.fields.map((f, i) => (
          <div key={i} className="card" style={{ marginBottom: "0.5rem" }}>
            <div className="param-row">
              <input aria-label="Value name" placeholder="order_number" value={f.name} onChange={(e) => setField(i, { name: e.target.value })} />
              <select aria-label="Type" value={f.type} onChange={(e) => setField(i, { type: e.target.value as WfField["type"] })}>
                <option value="string">text</option>
                <option value="integer">whole number</option>
                <option value="number">number</option>
                <option value="boolean">yes/no</option>
              </select>
              <input aria-label="Question" placeholder="What the assistant asks" value={f.ask} onChange={(e) => setField(i, { ask: e.target.value })} style={{ flex: 3 }} />
              {node.fields.length > 1 && (
                <button onClick={() => onChange({ ...node, fields: node.fields.filter((_, j) => j !== i) })}>Remove</button>
              )}
            </div>
            <div className="param-row">
              <input aria-label="Pattern" placeholder="must match (regex), optional" value={f.pattern ?? ""} onChange={(e) => setField(i, { pattern: e.target.value || null })} />
              <input
                aria-label="Allowed values"
                placeholder="allowed values (a, b, c), optional"
                value={(f.enum ?? []).join(", ")}
                onChange={(e) => {
                  const items = e.target.value.split(",").map((v) => v.trim()).filter(Boolean);
                  setField(i, { enum: items.length ? items : null });
                }}
              />
              <select aria-label="Tidy up" value={f.transform ?? ""} onChange={(e) => setField(i, { transform: (e.target.value || null) as WfField["transform"] })}>
                <option value="">keep as typed</option>
                <option value="upper">UPPERCASE</option>
                <option value="lower">lowercase</option>
              </select>
              <input aria-label="If invalid, say" placeholder="If it's invalid, say…" value={f.invalid_message} onChange={(e) => setField(i, { invalid_message: e.target.value })} />
            </div>
          </div>
        ))}
        <button onClick={() => onChange({ ...node, fields: [...node.fields, blankField()] })}>+ Ask for another detail</button>
        <p className="hint">
          If the customer gives details early (“return ACM-1002, it broke”), they are remembered and not asked
          again. After three failed tries the customer is passed to a person.
        </p>
        <TargetSelect label="Then" value={node.next} onChange={(v) => onChange({ ...node, next: v ?? "" })} stepIds={others} allowEnd={false} />
      </div>
    );
  }

  if (node.type === "action") {
    const action = actions.find((a) => a.name === node.action);
    const params = action ? Object.keys(action.parameters) : [];
    return (
      <div>
        <label>
          Action
          <select value={node.action} onChange={(e) => onChange({ ...node, action: e.target.value, args: {} })}>
            <option value="">Choose an action…</option>
            {actions.map((a) => (
              <option key={a.name} value={a.name}>
                {a.name} ({a.effect === "write" ? "changes data" : "reads"})
              </option>
            ))}
          </select>
        </label>
        {params.map((p) => (
          <label key={p}>
            {p}
            {action?.parameters[p].required ? " *" : ""}
            <input
              value={node.args[p] ?? ""}
              placeholder={`{{vars.${p}}}`}
              onChange={(e) => {
                const args = { ...node.args };
                if (e.target.value) args[p] = e.target.value;
                else delete args[p];
                onChange({ ...node, args });
              }}
            />
          </label>
        ))}
        {hint}
        <div className="user-form">
          <label>
            Keep the result as
            <input value={node.save_as ?? ""} placeholder="order" onChange={(e) => onChange({ ...node, save_as: e.target.value || null })} />
          </label>
          {action?.effect === "write" && (
            <label>
              Already approved by
              <select value={node.confirmed_by ?? ""} onChange={(e) => onChange({ ...node, confirmed_by: e.target.value || null })}>
                <option value="">Ask the customer to approve this action</option>
                {confirmIds.map((id) => (
                  <option key={id} value={id}>
                    the approval step “{id}”
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <div className="user-form">
          <TargetSelect label="Then" value={node.next} onChange={(v) => onChange({ ...node, next: v })} stepIds={others} allowEnd />
          <TargetSelect label="If it fails" value={node.on_error} onChange={(v) => onChange({ ...node, on_error: v })} stepIds={others} allowEnd />
        </div>
        <p className="hint">If it fails with no step chosen, the process stops and the customer is told it couldn&apos;t be completed.</p>
      </div>
    );
  }

  if (node.type === "branch") {
    return (
      <div>
        {node.cases.map((c, i) => (
          <div key={i} className="param-row">
            <span>If</span>
            <ConditionBuilder
              value={c.when}
              variables={variables}
              onChange={(when) => onChange({ ...node, cases: node.cases.map((x, j) => (j === i ? { ...x, when } : x)) })}
            />
            <TargetSelect label="go to" value={c.next} onChange={(v) => onChange({ ...node, cases: node.cases.map((x, j) => (j === i ? { ...x, next: v ?? "" } : x)) })} stepIds={others} allowEnd={false} />
            {node.cases.length > 1 && <button onClick={() => onChange({ ...node, cases: node.cases.filter((_, j) => j !== i) })}>Remove</button>}
          </div>
        ))}
        <button onClick={() => onChange({ ...node, cases: [...node.cases, { when: "", next: "" }] })}>+ Add condition</button>
        <TargetSelect label="Otherwise go to" value={node.otherwise} onChange={(v) => onChange({ ...node, otherwise: v })} stepIds={others} allowEnd />
        <p className="hint">Conditions are checked in order; the first that is true wins. A missing value never counts as true.</p>
      </div>
    );
  }

  if (node.type === "llm") {
    return (
      <div>
        <label>
          Instruction (the AI writes using only what you give it)
          <textarea rows={3} value={node.prompt} onChange={(e) => onChange({ ...node, prompt: e.target.value })} placeholder="Summarise {{vars.order}} in one friendly sentence." />
        </label>
        {hint}
        <div className="user-form">
          <label>
            Keep the text as
            <input value={node.save_as} onChange={(e) => onChange({ ...node, save_as: e.target.value })} placeholder="summary" />
          </label>
          <label>
            Or return several named values (comma separated)
            <input
              value={node.fields.join(", ")}
              onChange={(e) => onChange({ ...node, fields: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) })}
            />
          </label>
        </div>
        <TargetSelect label="Then" value={node.next} onChange={(v) => onChange({ ...node, next: v })} stepIds={others} allowEnd />
      </div>
    );
  }

  if (node.type === "knowledge") {
    return (
      <div>
        <label>
          Question to look up
          <input value={node.question} onChange={(e) => onChange({ ...node, question: e.target.value })} placeholder="What is the return policy?" />
        </label>
        {hint}
        <div className="user-form">
          <label className="inline">
            <input type="checkbox" checked={node.say} onChange={(e) => onChange({ ...node, say: e.target.checked })} />
            Tell the customer the answer
          </label>
          <label>
            Keep the answer as
            <input value={node.save_as ?? ""} onChange={(e) => onChange({ ...node, save_as: e.target.value || null })} />
          </label>
        </div>
        <TargetSelect label="Then" value={node.next} onChange={(v) => onChange({ ...node, next: v })} stepIds={others} allowEnd />
      </div>
    );
  }

  if (node.type === "message") {
    return (
      <div>
        <label>
          What the assistant says
          <textarea rows={3} value={node.text} onChange={(e) => onChange({ ...node, text: e.target.value })} />
        </label>
        {hint}
        <TargetSelect label="Then" value={node.next} onChange={(v) => onChange({ ...node, next: v })} stepIds={others} allowEnd />
      </div>
    );
  }

  if (node.type === "confirm") {
    return (
      <div>
        <label>
          What the customer is asked to approve
          <input value={node.summary} onChange={(e) => onChange({ ...node, summary: e.target.value })} placeholder="Return {{vars.order.item_name}} from order {{vars.order_number}}" />
        </label>
        {hint}
        <div className="user-form">
          <TargetSelect label="If they say yes" value={node.yes} onChange={(v) => onChange({ ...node, yes: v ?? "" })} stepIds={others} allowEnd={false} />
          <TargetSelect label="If they say no" value={node.no} onChange={(v) => onChange({ ...node, no: v ?? "" })} stepIds={others} allowEnd={false} />
        </div>
      </div>
    );
  }

  if (node.type === "handoff") {
    return (
      <div>
        <label>
          Why (for your team)
          <input value={node.reason} onChange={(e) => onChange({ ...node, reason: e.target.value })} />
        </label>
        <label>
          Summary of the situation
          <textarea rows={2} value={node.summary} onChange={(e) => onChange({ ...node, summary: e.target.value })} />
        </label>
        {hint}
        <p className="hint">The process ends here and your chatbot is told to bring in a person.</p>
      </div>
    );
  }

  return (
    <div>
      <label>
        How does it end?
        <select value={node.status} onChange={(e) => onChange({ ...node, status: e.target.value as "completed" | "failed" })}>
          <option value="completed">Successfully</option>
          <option value="failed">As a failure</option>
        </select>
      </label>
      <label>
        Final message (optional)
        <input value={node.message ?? ""} onChange={(e) => onChange({ ...node, message: e.target.value || null })} />
      </label>
    </div>
  );
}
