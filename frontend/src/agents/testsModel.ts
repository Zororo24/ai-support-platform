import type { ScenarioDefinition, ScenarioStep } from "../types";

export const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Any" },
  { value: "completed", label: "Answered / finished" },
  { value: "needs_input", label: "Asks for more details" },
  { value: "needs_confirmation", label: "Asks the customer to confirm" },
  { value: "pending_approval", label: "Waits for staff approval" },
  { value: "action_required", label: "Asks your backend to do something" },
  { value: "handed_off", label: "Hands over to a person" },
  { value: "failed", label: "Fails" },
];

/** A scenario step as the form edits it (text boxes instead of nested lists). */
export type StepForm = {
  kind: "say" | "confirm";
  text: string;
  confirm: "yes" | "no";
  status: string;
  outcome: string;
  contains: string;
  notContains: string;
  called: string; // one per line: `name` or `name {"argument": "value"}`
  notCalled: string;
  cites: "" | "yes" | "no";
  started: string;
};

export type ScenarioForm = {
  name: string;
  description: string;
  userId: string;
  attributes: string; // JSON
  workflow: string;
  mocks: string; // JSON
  steps: StepForm[];
};

export function blankStep(): StepForm {
  return {
    kind: "say",
    text: "",
    confirm: "yes",
    status: "",
    outcome: "",
    contains: "",
    notContains: "",
    called: "",
    notCalled: "",
    cites: "",
    started: "",
  };
}

export function blankForm(): ScenarioForm {
  return {
    name: "",
    description: "",
    userId: "test-customer",
    attributes: '{\n  "customer_id": "cust_1"\n}',
    workflow: "",
    mocks: "",
    steps: [blankStep()],
  };
}

const splitList = (text: string) =>
  text
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

export function formFromDefinition(name: string, def: ScenarioDefinition): ScenarioForm {
  return {
    name,
    description: def.description,
    userId: def.end_user.id,
    attributes: JSON.stringify(def.end_user.attributes, null, 2),
    workflow: def.workflow ?? "",
    mocks: Object.keys(def.mocks).length ? JSON.stringify(def.mocks, null, 2) : "",
    steps: def.steps.map((s) => ({
      kind: s.say !== null ? "say" : "confirm",
      text: s.say ?? "",
      confirm: s.confirm ?? "yes",
      status: s.expect.status ?? "",
      outcome: s.expect.outcome ?? "",
      contains: s.expect.reply_contains.join(", "),
      notContains: s.expect.reply_not_contains.join(", "),
      called: s.expect.actions_called
        .map((c) => (Object.keys(c.arguments).length ? `${c.name} ${JSON.stringify(c.arguments)}` : c.name))
        .join("\n"),
      notCalled: s.expect.actions_not_called.join(", "),
      cites: s.expect.cites === true ? "yes" : s.expect.cites === false ? "no" : "",
      started: s.expect.workflow_started ?? "",
    })),
  };
}

function parseObject(text: string, label: string): Record<string, unknown> {
  if (!text.trim()) return {};
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error(`${label} is not valid JSON.`);
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object like { "key": "value" }.`);
  }
  return value as Record<string, unknown>;
}

function parseCalls(text: string): { name: string; arguments: Record<string, unknown> }[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((line) => {
      const space = line.indexOf(" ");
      if (space === -1) return { name: line, arguments: {} };
      return {
        name: line.slice(0, space),
        arguments: parseObject(line.slice(space + 1), `The arguments for “${line.slice(0, space)}”`),
      };
    });
}

/** Throws an Error with a plain-language message when the form can't be turned into a scenario. */
export function definitionFromForm(form: ScenarioForm): ScenarioDefinition {
  const steps: ScenarioStep[] = form.steps.map((s, i) => {
    if (s.kind === "say" && !s.text.trim()) throw new Error(`Step ${i + 1} needs something for the customer to say.`);
    return {
      say: s.kind === "say" ? s.text.trim() : null,
      confirm: s.kind === "confirm" ? s.confirm : null,
      expect: {
        status: s.status || null,
        outcome: s.outcome.trim() || null,
        reply_contains: splitList(s.contains),
        reply_not_contains: splitList(s.notContains),
        cites: s.cites === "" ? null : s.cites === "yes",
        actions_called: parseCalls(s.called),
        actions_not_called: splitList(s.notCalled),
        workflow_started: s.started.trim() || null,
      },
    };
  });
  return {
    description: form.description.trim(),
    end_user: { id: form.userId.trim() || "test-customer", attributes: parseObject(form.attributes, "Customer details") },
    workflow: form.workflow || null,
    mocks: parseObject(form.mocks, "Simulated action results"),
    steps,
  };
}
