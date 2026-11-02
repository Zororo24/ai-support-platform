import { useState } from "react";

import type { ActionDef, Connection, ParamDef } from "../types";

export function blankAction(connectionId: string): ActionDef {
  return {
    name: "",
    description: "",
    execution: "hosted",
    connection_id: connectionId,
    request: { method: "GET", path: "/", query: {}, body: null, headers: {} },
    parameters: {},
    effect: "read",
    confirmation: null,
    confirm_summary: null,
    guards: [],
    limits: { timeout_s: 10, max_calls_per_run: 3, max_response_bytes: 65536, retries: 0 },
    response: { expose: [], max_chars: 4000 },
    idempotency_header: null,
    mock: null,
    enabled: true,
  };
}

type Pair = { key: string; value: string };

function toPairs(record: Record<string, string>): Pair[] {
  return Object.entries(record).map(([key, value]) => ({ key, value }));
}

function fromPairs(pairs: Pair[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const { key, value } of pairs) if (key.trim()) out[key.trim()] = value;
  return out;
}

type ParamRow = { name: string; def: ParamDef; enumText: string };

function parseJson(text: string, label: string): { value: unknown; error?: string } {
  if (!text.trim()) return { value: null };
  try {
    return { value: JSON.parse(text) };
  } catch {
    return { value: null, error: `${label} is not valid JSON` };
  }
}

export function ActionEditor({
  initial,
  connections,
  onSave,
  onCancel,
}: {
  initial: ActionDef;
  connections: Connection[];
  onSave: (action: ActionDef) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description);
  const [execution, setExecution] = useState(initial.execution ?? "hosted");
  const [connectionId, setConnectionId] = useState(initial.connection_id ?? "");
  const [effect, setEffect] = useState(initial.effect);
  const [confirmation, setConfirmation] = useState<"default" | "user" | "staff" | "none">(
    initial.confirmation ?? "default",
  );
  const [confirmSummary, setConfirmSummary] = useState(initial.confirm_summary ?? "");
  const [method, setMethod] = useState(initial.request?.method ?? "GET");
  const [path, setPath] = useState(initial.request?.path ?? "/");
  const [query, setQuery] = useState<Pair[]>(toPairs(initial.request?.query ?? {}));
  const [headers, setHeaders] = useState<Pair[]>(toPairs(initial.request?.headers ?? {}));
  const [body, setBody] = useState(initial.request?.body ? JSON.stringify(initial.request.body, null, 2) : "");
  const [params, setParams] = useState<ParamRow[]>(
    Object.entries(initial.parameters).map(([n, def]) => ({ name: n, def, enumText: (def.enum ?? []).join(", ") })),
  );
  const [guards, setGuards] = useState(initial.guards);
  const [expose, setExpose] = useState(initial.response.expose.join("\n"));
  const [maxChars, setMaxChars] = useState(initial.response.max_chars);
  const [timeout, setTimeoutS] = useState(initial.limits.timeout_s);
  const [maxCalls, setMaxCalls] = useState(initial.limits.max_calls_per_run);
  const [retries, setRetries] = useState(initial.limits.retries);
  const [idempotency, setIdempotency] = useState(initial.idempotency_header ?? "");
  const [mock, setMock] = useState(initial.mock ? JSON.stringify(initial.mock, null, 2) : "");
  const [error, setError] = useState<string | null>(null);

  const isWrite = effect === "write";
  const handback = execution === "handback";

  function submit() {
    const bodyParsed = parseJson(body, "Request body");
    const mockParsed = parseJson(mock, "Mock response");
    const problem = bodyParsed.error ?? mockParsed.error;
    if (problem) return setError(problem);
    if (!handback && !connectionId) return setError("Choose a connection first (add one under Connections).");

    const parameters: Record<string, ParamDef> = {};
    for (const row of params) {
      if (!row.name.trim()) continue;
      const items = row.enumText
        .split(",")
        .map((v) => v.trim())
        .filter(Boolean)
        .map((v) => (row.def.type === "integer" || row.def.type === "number" ? Number(v) : v));
      parameters[row.name.trim()] = { ...row.def, enum: items.length ? items : null };
    }

    onSave({
      name: name.trim(),
      description: description.trim(),
      execution,
      connection_id: handback ? null : connectionId,
      request: handback
        ? null
        : {
            method,
            path: path.trim(),
            query: fromPairs(query),
            body: method === "GET" ? null : bodyParsed.value,
            headers: fromPairs(headers),
          },
      parameters,
      effect,
      confirmation: confirmation === "default" ? null : confirmation,
      confirm_summary: isWrite && confirmSummary.trim() ? confirmSummary.trim() : null,
      guards: guards.filter((g) => g.when.trim()),
      limits: {
        ...initial.limits,
        timeout_s: timeout,
        max_calls_per_run: maxCalls,
        retries: isWrite ? 0 : retries,
      },
      response: {
        expose: expose.split("\n").map((l) => l.trim()).filter(Boolean),
        max_chars: maxChars,
      },
      idempotency_header: !handback && isWrite && idempotency.trim() ? idempotency.trim() : null,
      mock: mockParsed.value,
      enabled: initial.enabled,
    });
  }

  function updateParam(i: number, change: Partial<ParamRow>) {
    setParams((rows) => rows.map((r, j) => (j === i ? { ...r, ...change } : r)));
  }

  return (
    <div className="card action-editor">
      <h3>{initial.name ? `Edit action “${initial.name}”` : "New action"}</h3>

      <div className="user-form">
        <label>
          Name
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="get_order"
            maxLength={40}
          />
        </label>
        <label>
          Who performs it?
          <select value={execution} onChange={(e) => setExecution(e.target.value as "hosted" | "handback")}>
            <option value="hosted">We call your API (through a connection)</option>
            <option value="handback">Your own backend does it</option>
          </select>
        </label>
        {!handback && (
          <label>
            Connection
            <select value={connectionId} onChange={(e) => setConnectionId(e.target.value)}>
              <option value="">Select…</option>
              {connections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          What does it do?
          <select value={effect} onChange={(e) => setEffect(e.target.value as "read" | "write")}>
            <option value="read">Reads information (safe)</option>
            <option value="write">Changes something (asks the customer first)</option>
          </select>
        </label>
      </div>
      <label>
        Description (the AI reads this to decide when to use it)
        <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} />
      </label>

      {isWrite && (
        <div className="user-form">
          <label>
            Approval
            <select
              value={confirmation}
              onChange={(e) => setConfirmation(e.target.value as "default" | "user" | "staff" | "none")}
            >
              <option value="default">Ask the customer (recommended)</option>
              <option value="user">Always ask the customer</option>
              <option value="staff">A person on your team must approve</option>
              <option value="none">Never ask (runs immediately)</option>
            </select>
          </label>
          <label style={{ flex: 1, minWidth: "16rem" }}>
            What the customer is asked to approve
            <input
              value={confirmSummary}
              onChange={(e) => setConfirmSummary(e.target.value)}
              placeholder="Start a return for order {{args.order_number}}"
            />
          </label>
        </div>
      )}

      <h4>What the AI fills in</h4>
      <p className="hint">
        These are the details the AI collects from the conversation. Use them in the request below as{" "}
        <code>{"{{args.name}}"}</code>. Details about <em>who the customer is</em> aren&apos;t listed here: use{" "}
        <code>{"{{context.customer_id}}"}</code> and they are filled in from your system, never by the AI.
      </p>
      {params.map((row, i) => (
        <div key={i} className="param-row">
          <input
            aria-label="Parameter name"
            placeholder="order_number"
            value={row.name}
            onChange={(e) => updateParam(i, { name: e.target.value })}
          />
          <select
            aria-label="Type"
            value={row.def.type}
            onChange={(e) => updateParam(i, { def: { ...row.def, type: e.target.value as ParamDef["type"] } })}
          >
            <option value="string">text</option>
            <option value="integer">whole number</option>
            <option value="number">number</option>
            <option value="boolean">yes/no</option>
          </select>
          <input
            aria-label="Description"
            placeholder="Description for the AI"
            value={row.def.description}
            onChange={(e) => updateParam(i, { def: { ...row.def, description: e.target.value } })}
          />
          <input
            aria-label="Allowed values"
            placeholder="allowed values (a, b, c)"
            value={row.enumText}
            onChange={(e) => updateParam(i, { enumText: e.target.value })}
          />
          <input
            aria-label="Pattern"
            placeholder="pattern (regex)"
            value={row.def.pattern ?? ""}
            onChange={(e) => updateParam(i, { def: { ...row.def, pattern: e.target.value || null } })}
          />
          <label className="inline">
            <input
              type="checkbox"
              checked={row.def.required}
              onChange={(e) => updateParam(i, { def: { ...row.def, required: e.target.checked } })}
            />
            required
          </label>
          <button onClick={() => setParams((rows) => rows.filter((_, j) => j !== i))}>Remove</button>
        </div>
      ))}
      <button
        onClick={() =>
          setParams((rows) => [
            ...rows,
            {
              name: "",
              enumText: "",
              def: { type: "string", description: "", required: true, enum: null, pattern: null, min_length: null, max_length: null, minimum: null, maximum: null },
            },
          ])
        }
      >
        + Add detail
      </button>

      {handback && (
        <p className="hint">
          When the AI decides to use this, the response tells your chatbot&apos;s backend what to do (
          <code>status: &quot;action_required&quot;</code> with the details). Your server does it with its own
          credentials, then posts the outcome to <code>/v1/agents/…/tool-results</code> and the conversation
          continues. We never hold credentials for this system.
        </p>
      )}
      {!handback && <h4>The request to your system</h4>}
      {!handback && (
      <div className="user-form">
        <label>
          Method
          <select value={method} onChange={(e) => setMethod(e.target.value as NonNullable<ActionDef["request"]>["method"])}>
            {["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => (
              <option key={m}>{m}</option>
            ))}
          </select>
        </label>
        <label style={{ flex: 1, minWidth: "16rem" }}>
          Path (added to the connection&apos;s address)
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="/orders/{{args.order_number}}" />
        </label>
      </div>
      )}
      {!handback && (
        <>
          <PairEditor label="Query parameters" pairs={query} onChange={setQuery} keyPlaceholder="customer_id" valuePlaceholder="{{context.customer_id}}" />
          {method !== "GET" && (
            <label>
              Body (JSON)
              <textarea rows={5} value={body} onChange={(e) => setBody(e.target.value)} placeholder={'{"order_number": "{{args.order_number}}"}'} />
            </label>
          )}
          <PairEditor label="Extra headers" pairs={headers} onChange={setHeaders} keyPlaceholder="X-Source" valuePlaceholder="support-bot" />
        </>
      )}

      <h4>What the AI sees back</h4>
      <label>
        Fields to show (one per line, e.g. <code>$.status</code>). Leave empty to show everything.{" "}
        {handback && "These are picked from the result your backend sends."}
        <textarea rows={3} value={expose} onChange={(e) => setExpose(e.target.value)} placeholder={"$.status\n$.delivered_at"} />
      </label>

      <h4>Safety rules</h4>
      <p className="hint">
        Rules are checked by the platform before anything is sent, whatever the AI decides. Example:{" "}
        <code>args.amount_cents &gt; 20000</code> blocks refunds over $200.
      </p>
      {guards.map((g, i) => (
        <div key={i} className="param-row">
          <input
            aria-label="Block when"
            placeholder="block when… e.g. args.amount > 100"
            value={g.when}
            onChange={(e) => setGuards((rows) => rows.map((r, j) => (j === i ? { ...r, when: e.target.value } : r)))}
          />
          <input
            aria-label="Message"
            placeholder="Message shown to the AI"
            value={g.message}
            onChange={(e) => setGuards((rows) => rows.map((r, j) => (j === i ? { ...r, message: e.target.value } : r)))}
          />
          <button onClick={() => setGuards((rows) => rows.filter((_, j) => j !== i))}>Remove</button>
        </div>
      ))}
      <button onClick={() => setGuards((rows) => [...rows, { when: "", then: "refuse", message: "That request isn't allowed." }])}>
        + Add rule
      </button>

      <h4>Limits & testing</h4>
      <div className="user-form">
        <label>
          Timeout (seconds)
          <input type="number" min={1} max={30} value={timeout} onChange={(e) => setTimeoutS(Number(e.target.value) || 10)} />
        </label>
        <label>
          Max uses per conversation turn
          <input type="number" min={1} max={10} value={maxCalls} onChange={(e) => setMaxCalls(Number(e.target.value) || 3)} />
        </label>
        {!isWrite && (
          <label>
            Retries on temporary failure
            <input type="number" min={0} max={2} value={retries} onChange={(e) => setRetries(Number(e.target.value) || 0)} />
          </label>
        )}
        {isWrite && !handback && (
          <label>
            Idempotency header (safe retries)
            <input value={idempotency} onChange={(e) => setIdempotency(e.target.value)} placeholder="Idempotency-Key" />
          </label>
        )}
        <label>
          Max characters shown to the AI
          <input type="number" min={200} max={20000} value={maxChars} onChange={(e) => setMaxChars(Number(e.target.value) || 4000)} />
        </label>
      </div>
      <label>
        Sample response for testing without calling your system (JSON)
        <textarea rows={4} value={mock} onChange={(e) => setMock(e.target.value)} />
      </label>

      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button className="primary" onClick={submit} disabled={!name.trim() || !description.trim()}>
          Keep this action
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
      <p className="hint">Changes apply to the draft. Use “Save draft” at the top, then test, then publish.</p>
    </div>
  );
}

function PairEditor({
  label,
  pairs,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
}: {
  label: string;
  pairs: Pair[];
  onChange: (pairs: Pair[]) => void;
  keyPlaceholder: string;
  valuePlaceholder: string;
}) {
  return (
    <div>
      <strong style={{ fontSize: "0.85rem" }}>{label}</strong>
      {pairs.map((p, i) => (
        <div key={i} className="param-row">
          <input
            placeholder={keyPlaceholder}
            value={p.key}
            onChange={(e) => onChange(pairs.map((r, j) => (j === i ? { ...r, key: e.target.value } : r)))}
          />
          <input
            placeholder={valuePlaceholder}
            value={p.value}
            onChange={(e) => onChange(pairs.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))}
          />
          <button onClick={() => onChange(pairs.filter((_, j) => j !== i))}>Remove</button>
        </div>
      ))}
      <div>
        <button onClick={() => onChange([...pairs, { key: "", value: "" }])}>+ Add</button>
      </div>
    </div>
  );
}
