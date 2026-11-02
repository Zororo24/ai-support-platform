import { useState } from "react";

import { testAction } from "../api/client";
import type { ActionDef, ActionTestResult } from "../types";

function parseObject(text: string, label: string): { value: Record<string, unknown>; error?: string } {
  if (!text.trim()) return { value: {} };
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return { value: parsed as Record<string, unknown> };
    }
    return { value: {}, error: `${label} must be a JSON object` };
  } catch {
    return { value: {}, error: `${label} is not valid JSON` };
  }
}

export function ActionTester({ agentId, action, dirty }: { agentId: string; action: ActionDef; dirty: boolean }) {
  const sampleArgs = Object.fromEntries(
    Object.entries(action.parameters).map(([k, p]) => [k, p.type === "integer" || p.type === "number" ? 1 : "sample"]),
  );
  const [args, setArgs] = useState(JSON.stringify(sampleArgs, null, 2));
  const [context, setContext] = useState('{\n  "customer_id": "cust_1"\n}');
  const handback = action.execution === "handback";
  const [useMock, setUseMockState] = useState(handback); // your backend performs it: only the sample can run here
  const setUseMock = (value: boolean) => setUseMockState(handback || value);
  const [forReal, setForReal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ActionTestResult | null>(null);

  async function run() {
    const a = parseObject(args, "Details");
    const c = parseObject(context, "Customer attributes");
    if (a.error || c.error) return setError(a.error ?? c.error ?? null);
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await testAction(agentId, action.name, {
          arguments: a.value,
          context: c.value,
          mock: useMock,
          send_for_real: forReal,
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "The test failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h4 style={{ marginTop: 0 }}>Test “{action.name}”</h4>
      {dirty && <p className="hint">Tests use the saved draft. Save your latest edits first.</p>}
      <div className="user-form">
        <label style={{ flex: 1, minWidth: "14rem" }}>
          Details the AI would fill in (JSON)
          <textarea rows={4} value={args} onChange={(e) => setArgs(e.target.value)} />
        </label>
        <label style={{ flex: 1, minWidth: "14rem" }}>
          Customer attributes from your system (JSON)
          <textarea rows={4} value={context} onChange={(e) => setContext(e.target.value)} />
        </label>
      </div>
      <div className="actions" style={{ alignItems: "center" }}>
        <label className="inline">
          <input
            type="checkbox"
            checked={useMock}
            disabled={handback}
            onChange={(e) => setUseMock(e.target.checked)}
          />
          use the sample response ({handback ? "your backend performs this action" : "don't call my system"})
        </label>
        {action.effect === "write" && !useMock && (
          <label className="inline">
            <input type="checkbox" checked={forReal} onChange={(e) => setForReal(e.target.checked)} />
            send for real (this changes data)
          </label>
        )}
        <button className="primary" disabled={busy} onClick={() => void run()}>
          {busy ? "Running…" : "Run test"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {result?.refused && (
        <p className="error">
          Blocked before sending ({result.refused.code}): {result.refused.message}
        </p>
      )}
      {result?.request && (
        <>
          <strong>Request sent</strong>
          <pre className="code">{`${result.request.method} ${result.request.url}\n${JSON.stringify(
            { query: result.request.query, headers: result.request.headers, body: result.request.body },
            null,
            2,
          )}`}</pre>
        </>
      )}
      {result?.result && (
        <p className={result.result.ok ? "notice" : "error"}>
          {result.result.mock ? "Sample response" : result.result.ok ? "Success" : "Failed"}
          {result.result.status_code ? ` · HTTP ${result.result.status_code}` : ""}
          {!result.result.mock ? ` · ${result.result.latency_ms} ms` : ""}
          {result.result.error ? ` · ${result.result.error}` : ""}
        </p>
      )}
      {result?.model_view && (
        <>
          <strong>What the AI would see</strong>
          <pre className="code">{JSON.stringify(result.model_view, null, 2)}</pre>
        </>
      )}
    </div>
  );
}
