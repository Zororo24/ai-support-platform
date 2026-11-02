import type { WfField, WfNode, WorkflowDef } from "../types";

export const NODE_LABELS: Record<WfNode["type"], string> = {
  collect: "Ask the customer for details",
  action: "Do something in your system",
  branch: "Choose a path",
  llm: "Write a short text",
  knowledge: "Answer from your documents",
  message: "Say something",
  confirm: "Ask the customer to approve",
  handoff: "Pass to a person",
  end: "Finish",
};

export function blankField(): WfField {
  return {
    name: "",
    type: "string",
    ask: "",
    invalid_message: "That doesn't look right.",
    pattern: null,
    enum: null,
    transform: null,
    max_length: 500,
    minimum: null,
    maximum: null,
  };
}

export function blankNode(type: WfNode["type"], firstTarget: string | null): WfNode {
  switch (type) {
    case "collect":
      return { type, fields: [blankField()], next: firstTarget ?? "" };
    case "action":
      return { type, action: "", args: {}, save_as: null, on_error: null, confirmed_by: null, next: null };
    case "branch":
      return { type, cases: [{ when: "", next: firstTarget ?? "" }], otherwise: null };
    case "llm":
      return { type, prompt: "", save_as: "", fields: [], next: null };
    case "knowledge":
      return { type, question: "", save_as: null, say: true, next: null };
    case "message":
      return { type, text: "", next: null };
    case "confirm":
      return { type, summary: "", yes: firstTarget ?? "", no: firstTarget ?? "" };
    case "handoff":
      return { type, reason: "", summary: "" };
    case "end":
      return { type, status: "completed", message: null };
  }
}

/** Where a step can lead next (for the flow outline and the reachability hint). */
export function targets(node: WfNode): (string | null)[] {
  switch (node.type) {
    case "collect":
    case "message":
    case "llm":
    case "knowledge":
      return [node.next];
    case "action":
      return [node.next, node.on_error];
    case "branch":
      return [...node.cases.map((c) => c.next), node.otherwise];
    case "confirm":
      return [node.yes, node.no];
    default:
      return [];
  }
}

/** Values that exist at some point in the process: what steps may refer to as vars.<name>. */
export function knownVars(wf: WorkflowDef): string[] {
  const out = new Set<string>(["last_error"]);
  for (const node of Object.values(wf.nodes)) {
    if (node.type === "collect") node.fields.forEach((f) => f.name && out.add(f.name));
    if ((node.type === "action" || node.type === "llm" || node.type === "knowledge") && node.save_as) out.add(node.save_as);
  }
  return [...out];
}

export function describe(node: WfNode): string {
  switch (node.type) {
    case "collect":
      return `Ask for ${node.fields.map((f) => f.name || "…").join(", ")}`;
    case "action":
      return `Run ${node.action || "…"}${node.confirmed_by ? " (already approved)" : ""}`;
    case "branch":
      return `Choose a path (${node.cases.length} condition${node.cases.length === 1 ? "" : "s"})`;
    case "llm":
      return "Write a short text";
    case "knowledge":
      return "Answer from documents";
    case "message":
      return `Say: “${node.text.slice(0, 40)}${node.text.length > 40 ? "…" : ""}”`;
    case "confirm":
      return "Ask the customer to approve";
    case "handoff":
      return "Pass to a person";
    case "end":
      return node.status === "failed" ? "Finish (failed)" : "Finish";
  }
}

/** A readable outline of the process, in the order steps can be reached from the start. */
export function outline(wf: WorkflowDef): { id: string; text: string; goes: string[] }[] {
  const seen = new Set<string>();
  const order: string[] = [];
  const queue = [wf.start];
  while (queue.length) {
    const id = queue.shift() as string;
    if (!id || seen.has(id) || !wf.nodes[id]) continue;
    seen.add(id);
    order.push(id);
    for (const t of targets(wf.nodes[id])) if (t) queue.push(t);
  }
  const unreachable = Object.keys(wf.nodes).filter((id) => !seen.has(id));
  return [...order, ...unreachable].map((id) => {
    const node = wf.nodes[id];
    const goes: string[] = [];
    if (node.type === "branch") {
      node.cases.forEach((c) => goes.push(`if ${c.when || "…"} → ${c.next || "?"}`));
      goes.push(`otherwise → ${node.otherwise ?? "end"}`);
    } else if (node.type === "confirm") {
      goes.push(`yes → ${node.yes || "?"}`, `no → ${node.no || "?"}`);
    } else if (node.type === "action") {
      goes.push(`→ ${node.next ?? "end"}`);
      if (node.on_error) goes.push(`on error → ${node.on_error}`);
    } else if ("next" in node) {
      goes.push(`→ ${node.next ?? "end"}`);
    }
    return { id, text: describe(node), goes: unreachable.includes(id) ? ["(never reached)", ...goes] : goes };
  });
}

export function blankWorkflow(): WorkflowDef {
  return {
    name: "",
    description: "",
    start: "start",
    enabled: true,
    nodes: { start: { type: "message", text: "Hello!", next: null } },
  };
}
