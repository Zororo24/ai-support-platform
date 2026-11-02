import type { AgentConfig } from "../types";

type Props = {
  config: AgentConfig;
  setConfig: (config: AgentConfig) => void;
};

function numberValue(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function InstructionsTab({ config, setConfig }: Props) {
  const set = (changes: Partial<AgentConfig>) => setConfig({ ...config, ...changes });

  return (
    <div className="instructions">
      <div className="card">
        <h3>Instructions</h3>
        <p className="hint">
          Tell the agent how to behave: tone, what it should and shouldn&apos;t do, how to address customers.
          Answers still come only from your knowledge; instructions can&apos;t make it invent facts.
        </p>
        <label>
          Instructions
          <textarea
            rows={8}
            maxLength={8000}
            value={config.instructions}
            onChange={(e) => set({ instructions: e.target.value })}
            placeholder="e.g. You are Acme's friendly support assistant. Keep answers under three sentences."
          />
        </label>
        <label>
          Language
          <input
            maxLength={16}
            value={config.language}
            onChange={(e) => set({ language: e.target.value })}
            style={{ maxWidth: "8rem" }}
          />
        </label>
        <label>
          When the documents don&apos;t cover a question, say
          <input
            maxLength={500}
            value={config.knowledge.not_found_message}
            onChange={(e) =>
              set({ knowledge: { ...config.knowledge, not_found_message: e.target.value } })
            }
          />
        </label>
      </div>

      <div className="card">
        <h3>Limits</h3>
        <p className="hint">Protect your usage allowance. Requests over a limit get HTTP 429 from the API.</p>
        <div className="user-form">
          <label>
            Requests per minute, per API key
            <input
              type="number"
              min={1}
              max={6000}
              value={config.limits.requests_per_minute_per_key}
              onChange={(e) =>
                set({
                  limits: {
                    ...config.limits,
                    requests_per_minute_per_key: numberValue(e.target.value, 60),
                  },
                })
              }
            />
          </label>
          <label>
            Requests per minute, per end user
            <input
              type="number"
              min={1}
              max={600}
              value={config.limits.requests_per_minute_per_end_user}
              onChange={(e) =>
                set({
                  limits: {
                    ...config.limits,
                    requests_per_minute_per_end_user: numberValue(e.target.value, 20),
                  },
                })
              }
            />
          </label>
          <label>
            Simultaneous requests, per API key
            <input
              type="number"
              min={1}
              max={100}
              value={config.limits.max_concurrent_requests}
              onChange={(e) =>
                set({
                  limits: { ...config.limits, max_concurrent_requests: numberValue(e.target.value, 5) },
                })
              }
            />
          </label>
        </div>
        <label>
          Daily AI budget (tokens, blank for no limit)
          <input
            type="number"
            min={1000}
            step={1000}
            value={config.limits.daily_token_budget ?? ""}
            placeholder="No limit"
            style={{ maxWidth: "12rem" }}
            onChange={(e) =>
              set({
                limits: {
                  ...config.limits,
                  daily_token_budget: e.target.value === "" ? null : Number(e.target.value),
                },
              })
            }
          />
        </label>
        <p className="hint">
          When an agent uses its day&apos;s budget (UTC), new messages get HTTP 429 <code>budget_exceeded</code>{" "}
          until midnight. You are warned by webhook at 80% and again at 100%.
        </p>
      </div>

      <div className="card">
        <h3>Privacy</h3>
        <label>
          Conversation storage
          <select
            value={config.privacy.store_transcript}
            onChange={(e) =>
              set({
                privacy: { ...config.privacy, store_transcript: e.target.value as "full" | "none" },
              })
            }
          >
            <option value="full">Store conversations (enables insights and review)</option>
            <option value="none">Don&apos;t store conversation text</option>
          </select>
        </label>
        <div className="user-form">
          <label>
            Delete conversations after (days without activity)
            <input
              type="number"
              min={1}
              max={365}
              value={config.privacy.retention_days}
              style={{ maxWidth: "8rem" }}
              onChange={(e) =>
                set({ privacy: { ...config.privacy, retention_days: numberValue(e.target.value, 30) } })
              }
            />
          </label>
          <label className="inline">
            <input
              type="checkbox"
              checked={config.privacy.redact_pii}
              onChange={(e) => set({ privacy: { ...config.privacy, redact_pii: e.target.checked } })}
            />
            <span>
              Hide emails, card numbers and phone numbers in stored conversations. The AI still sees them while
              it helps; this is pattern matching and will not catch names or addresses.
            </span>
          </label>
        </div>
        <p className="hint">
          Either way, message text is sent to the AI model provider to produce answers. You can delete a
          customer&apos;s data through the API at any time.
        </p>
      </div>
    </div>
  );
}
