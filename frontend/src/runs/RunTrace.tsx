import type { RunDetailInfo, RunStepInfo } from "../types";
import { formatDate } from "../admin/format";

const STEP_LABELS: Record<string, string> = {
  llm: "Model decision",
  retrieve: "Searched knowledge",
  answer: "Wrote an answer",
  action: "Action",
  policy: "Blocked by rules",
  confirm: "Approval",
  approval: "Staff approval",
  handback: "Your backend",
  workflow: "Process",
  handoff: "Hand-off",
};

export function statusBadgeClass(status: string): string {
  if (status === "completed" || status === "handed_off") return "doc-ready";
  if (status === "failed") return "doc-failed";
  return "doc-pending";
}

function Json({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <small className="hint">none</small>;
  return <pre className="code">{JSON.stringify(value, null, 2)}</pre>;
}

function Step({ step }: { step: RunStepInfo }) {
  const failed = step.status === "error";
  return (
    <li className={`trace-step ${failed ? "failed" : ""}`}>
      <details>
        <summary>
          <span className="seq">{step.seq}</span>
          <span className="kind">{STEP_LABELS[step.type] ?? step.type}</span>
          <strong>{step.name}</strong>
          {failed && <span className="badge doc-failed">problem</span>}
          <small className="metrics">
            {step.latency_ms} ms{step.tokens > 0 ? ` · ${step.tokens} tokens` : ""}
          </small>
        </summary>
        {step.error && <p className="error">{step.error}</p>}
        <div className="io">
          <div>
            <small>Input</small>
            <Json value={step.input} />
          </div>
          <div>
            <small>Output</small>
            <Json value={step.output} />
          </div>
        </div>
      </details>
    </li>
  );
}

export function RunTrace({ run }: { run: RunDetailInfo }) {
  return (
    <div className="run-trace">
      <div className="doc-head">
        <h3 style={{ margin: 0 }}>{run.input_preview ?? "(no message)"}</h3>
        <span className={`badge ${statusBadgeClass(run.status)}`}>{run.status.replace("_", " ")}</span>
        {run.outcome && <span className="badge">{run.outcome}</span>}
      </div>
      <dl className="meta">
        <dt>Agent</dt>
        <dd>
          {run.agent_name}
          {run.agent_version !== null ? ` v${run.agent_version}` : " (draft)"}
        </dd>
        <dt>Customer</dt>
        <dd>
          {run.end_user_ref} · session {run.session_id} · {run.source}
        </dd>
        <dt>When</dt>
        <dd>
          {formatDate(run.started_at)}
          {run.duration_ms !== null ? ` · ${run.duration_ms} ms` : ""} · {run.tokens} tokens · {run.actions} action
          {run.actions === 1 ? "" : "s"}
        </dd>
        {run.reply_preview && (
          <>
            <dt>Reply</dt>
            <dd>{run.reply_preview}</dd>
          </>
        )}
      </dl>
      {run.error && <p className="error">Failed: {run.error}</p>}
      {run.pending && (
        <div className="secret-box">
          <strong>Waiting for the customer to approve: {run.pending.summary}</strong>
          <pre className="code">{JSON.stringify(run.pending.args_preview, null, 2)}</pre>
          <small className="hint">Expires {formatDate(run.pending.expires_at)}</small>
        </div>
      )}
      <ol className="trace">
        {run.steps.map((s) => (
          <Step key={s.seq} step={s} />
        ))}
      </ol>
      {run.steps.length === 0 && <p className="hint">No steps were recorded.</p>}
    </div>
  );
}
