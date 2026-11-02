import { useState } from "react";

const OPERATORS: { value: string; label: string }[] = [
  { value: "<=", label: "is at most" },
  { value: "<", label: "is less than" },
  { value: ">=", label: "is at least" },
  { value: ">", label: "is more than" },
  { value: "==", label: "is exactly" },
  { value: "!=", label: "is not" },
];

const SIMPLE = /^\s*((?:vars|context)\.[A-Za-z0-9_.[\]]+)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$/;

type Parts = { variable: string; op: string; value: string };

function parse(expression: string): Parts | null {
  const match = SIMPLE.exec(expression);
  if (!match) return null;
  const raw = match[3];
  const quoted = /^(['"])(.*)\1$/.exec(raw);
  return { variable: match[1], op: match[2], value: quoted ? quoted[2] : raw };
}

function build({ variable, op, value }: Parts): string {
  const isLiteral = /^-?\d+(\.\d+)?$/.test(value) || value === "true" || value === "false" || value === "null";
  return `${variable} ${op} ${isLiteral ? value : `"${value.replace(/"/g, "'")}"`}`;
}

/**
 * Lets people write "the order's delivery age is at most 30" by picking from lists. Anything the
 * builder cannot express is kept as typed text, so nothing is ever lost.
 */
export function ConditionBuilder({
  value,
  onChange,
  variables,
}: {
  value: string;
  onChange: (expression: string) => void;
  variables: string[];
}) {
  const parsed = parse(value);
  const [advanced, setAdvanced] = useState(value.trim() !== "" && parsed === null);

  if (advanced) {
    return (
      <div className="condition">
        <input
          aria-label="Condition"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder='vars.order.days_since_delivery <= 30 and context.plan == "pro"'
        />
        {parsed !== null || value.trim() === "" ? (
          <button onClick={() => setAdvanced(false)}>Simple</button>
        ) : (
          <small className="hint">Too complex for the simple builder</small>
        )}
      </div>
    );
  }

  const current = parsed ?? { variable: variables[0] ? `vars.${variables[0]}` : "", op: "<=", value: "" };
  const options = [...new Set([...variables.map((v) => `vars.${v}`), current.variable].filter(Boolean))];
  const update = (change: Partial<Parts>) => onChange(build({ ...current, ...change }));

  return (
    <div className="condition">
      <input
        aria-label="Value to check"
        list="wf-variables"
        value={current.variable}
        onChange={(e) => update({ variable: e.target.value })}
        placeholder="vars.order.days_since_delivery"
      />
      <datalist id="wf-variables">
        {options.map((o) => (
          <option key={o} value={o} />
        ))}
      </datalist>
      <select aria-label="Comparison" value={current.op} onChange={(e) => update({ op: e.target.value })}>
        {OPERATORS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <input aria-label="Compared with" value={current.value} onChange={(e) => update({ value: e.target.value })} placeholder="30" />
      <button onClick={() => setAdvanced(true)}>Advanced</button>
    </div>
  );
}
