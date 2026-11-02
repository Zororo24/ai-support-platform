import type { ConfigDiff } from "../types";

function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value.length > 400 ? `${value.slice(0, 400)}…` : value;
  return JSON.stringify(value);
}

/** What differs between two configurations, grouped by area. */
export function DiffView({ diff }: { diff: ConfigDiff }) {
  if (diff.identical) return <p className="hint">No differences between {diff.from} and {diff.to}.</p>;
  return (
    <div className="diff">
      {diff.sections.map((section) => (
        <div key={section.title}>
          <h4 style={{ marginBottom: "0.25rem" }}>{section.title}</h4>
          <ul style={{ paddingLeft: "1.1rem", margin: 0 }}>
            {section.changes.map((c, i) => (
              <li key={i} style={{ marginBottom: "0.35rem" }}>
                <span
                  className={`badge ${c.kind === "added" ? "doc-ready" : c.kind === "removed" ? "doc-failed" : "doc-pending"}`}
                >
                  {c.kind}
                </span>{" "}
                <strong>{c.label}</strong>
                {c.kind === "changed" && (
                  <div className="hint">
                    <span style={{ textDecoration: "line-through" }}>{show(c.before)}</span> → {show(c.after)}
                  </div>
                )}
                {c.kind === "added" && c.after !== null && c.after !== undefined && (
                  <div className="hint">{show(c.after)}</div>
                )}
                {c.kind === "removed" && c.before !== null && c.before !== undefined && (
                  <div className="hint">{show(c.before)}</div>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
