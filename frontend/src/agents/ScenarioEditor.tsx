import { useState } from "react";

import type { AgentConfig, Scenario } from "../types";
import {
  STATUS_OPTIONS,
  blankForm,
  blankStep,
  definitionFromForm,
  formFromDefinition,
} from "./testsModel";
import type { ScenarioForm, StepForm } from "./testsModel";

type Props = {
  config: AgentConfig;
  existing: Scenario | null;
  onSave: (name: string, definition: ReturnType<typeof definitionFromForm>) => Promise<void>;
  onCancel: () => void;
};

export function ScenarioEditor({ config, existing, onSave, onCancel }: Props) {
  const [form, setForm] = useState<ScenarioForm>(
    existing ? formFromDefinition(existing.name, existing.definition) : blankForm(),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const actionNames = config.actions.map((a) => a.name);
  const set = (change: Partial<ScenarioForm>) => setForm({ ...form, ...change });
  const setStep = (i: number, change: Partial<StepForm>) =>
    set({ steps: form.steps.map((s, j) => (j === i ? { ...s, ...change } : s)) });

  async function submit() {
    setError(null);
    let definition;
    try {
      definition = definitionFromForm(form);
    } catch (e) {
      return setError(e instanceof Error ? e.message : "Check the test and try again.");
    }
    setBusy(true);
    try {
      await onSave(form.name.trim(), definition);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the test");
      setBusy(false);
    }
  }

  return (
    <div className="card scenario-editor">
      <h3>{existing ? `Edit “${existing.name}”` : "New test"}</h3>
      <div className="user-form">
        <label style={{ flex: 1, minWidth: "14rem" }}>
          Name
          <input value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={120} placeholder="Customer asks where their order is" />
        </label>
        <label style={{ flex: 2, minWidth: "16rem" }}>
          What this checks (optional)
          <input value={form.description} onChange={(e) => set({ description: e.target.value })} maxLength={500} />
        </label>
      </div>

      <h4>The customer</h4>
      <div className="user-form">
        <label>
          Customer id
          <input value={form.userId} onChange={(e) => set({ userId: e.target.value })} maxLength={100} />
        </label>
        <label style={{ flex: 1, minWidth: "16rem" }}>
          Customer details (JSON), as your system would send them
          <textarea rows={3} value={form.attributes} onChange={(e) => set({ attributes: e.target.value })} />
        </label>
        {config.workflows.length > 0 && (
          <label>
            Start this process first
            <select value={form.workflow} onChange={(e) => set({ workflow: e.target.value })}>
              <option value="">Let the agent decide</option>
              {config.workflows.map((w) => (
                <option key={w.name} value={w.name}>
                  {w.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <h4>What your systems return</h4>
      <p className="hint">
        By default each action answers with its sample response. To test a special case, say what an action
        returns here, for example an order that is too old:{" "}
        <code>{`{ "get_order": { "result": { "days_since_delivery": 45 } } }`}</code> or a failure:{" "}
        <code>{`{ "get_order": { "error": "timed out" } }`}</code>.
        {actionNames.length > 0 && <> Your actions: {actionNames.join(", ")}.</>}
      </p>
      <textarea rows={form.mocks ? 5 : 2} value={form.mocks} onChange={(e) => set({ mocks: e.target.value })} placeholder="Leave empty to use the sample responses" />

      <h4>The conversation</h4>
      {form.steps.map((s, i) => (
        <div className="card" key={i} style={{ background: "#fafbfc" }}>
          <div className="user-form">
            <label>
              Step {i + 1}
              <select value={s.kind} onChange={(e) => setStep(i, { kind: e.target.value as StepForm["kind"] })}>
                <option value="say">The customer writes</option>
                <option value="confirm">The customer presses Yes / No</option>
              </select>
            </label>
            {s.kind === "say" ? (
              <label style={{ flex: 1, minWidth: "16rem" }}>
                Message
                <input value={s.text} onChange={(e) => setStep(i, { text: e.target.value })} maxLength={2000} />
              </label>
            ) : (
              <label>
                Button
                <select value={s.confirm} onChange={(e) => setStep(i, { confirm: e.target.value as "yes" | "no" })}>
                  <option value="yes">Yes</option>
                  <option value="no">No</option>
                </select>
              </label>
            )}
            <button disabled={form.steps.length === 1} onClick={() => set({ steps: form.steps.filter((_, j) => j !== i) })}>
              Remove step
            </button>
          </div>
          <details open={Boolean(s.status || s.contains || s.called || s.notCalled || s.notContains || s.started || s.cites)}>
            <summary>What should happen? (checks)</summary>
            <div className="user-form">
              <label>
                The agent…
                <select value={s.status} onChange={(e) => setStep(i, { status: e.target.value })}>
                  {STATUS_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Cites a document
                <select value={s.cites} onChange={(e) => setStep(i, { cites: e.target.value as StepForm["cites"] })}>
                  <option value="">Doesn&apos;t matter</option>
                  <option value="yes">Yes</option>
                  <option value="no">No</option>
                </select>
              </label>
              {config.workflows.length > 0 && (
                <label>
                  Starts the process
                  <select value={s.started} onChange={(e) => setStep(i, { started: e.target.value })}>
                    <option value="">Doesn&apos;t matter</option>
                    {config.workflows.map((w) => (
                      <option key={w.name} value={w.name}>
                        {w.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            <div className="user-form">
              <label style={{ flex: 1, minWidth: "14rem" }}>
                The reply mentions (comma separated)
                <input value={s.contains} onChange={(e) => setStep(i, { contains: e.target.value })} placeholder="delivered, 12 days" />
              </label>
              <label style={{ flex: 1, minWidth: "14rem" }}>
                The reply does not mention
                <input value={s.notContains} onChange={(e) => setStep(i, { notContains: e.target.value })} />
              </label>
            </div>
            <div className="user-form">
              <label style={{ flex: 1, minWidth: "16rem" }}>
                Actions that must be used (one per line; add details after the name if they matter)
                <textarea
                  rows={2}
                  value={s.called}
                  onChange={(e) => setStep(i, { called: e.target.value })}
                  placeholder={'get_order {"order_number": "ACM-1002"}'}
                />
              </label>
              <label style={{ flex: 1, minWidth: "14rem" }}>
                Actions that must NOT be used (comma separated)
                <input value={s.notCalled} onChange={(e) => setStep(i, { notCalled: e.target.value })} placeholder="create_return" />
              </label>
            </div>
          </details>
        </div>
      ))}
      <button disabled={form.steps.length >= 12} onClick={() => set({ steps: [...form.steps, blankStep()] })}>
        + Add a step
      </button>

      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button className="primary" disabled={busy || !form.name.trim()} onClick={() => void submit()}>
          {busy ? "Saving…" : "Save test"}
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
