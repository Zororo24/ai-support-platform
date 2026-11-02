import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { listAgents, testRetrieval } from "../api/client";
import type { AgentSummary, RetrievalTestResult } from "../types";

export function RetrievalTester() {
  const [query, setQuery] = useState("");
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentId, setAgentId] = useState("");
  const [rerank, setRerank] = useState<"agent" | "on" | "off">("agent");

  useEffect(() => {
    listAgents()
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);
  const [result, setResult] = useState<RetrievalTestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setResult(
        await testRetrieval(query.trim(), agentId || undefined, rerank === "agent" ? undefined : rerank === "on"),
      );
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : "Test failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <p className="hint">
        Type a customer question to see which passages the assistant would be given, and whether
        they matched by meaning (dense), by keywords, or both.
      </p>
      <label>
        Search as
        <select value={agentId} onChange={(e) => setAgentId(e.target.value)} style={{ maxWidth: "24rem" }}>
          <option value="">The whole knowledge base</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              Agent: {a.name} (its collections and settings)
            </option>
          ))}
        </select>
      </label>
      <label>
        Double-check with the AI
        <select value={rerank} onChange={(e) => setRerank(e.target.value as "agent" | "on" | "off")} style={{ maxWidth: "24rem" }}>
          <option value="agent">Use the agent&apos;s setting</option>
          <option value="on">On</option>
          <option value="off">Off</option>
        </select>
      </label>
      <form className="composer" onSubmit={onSubmit}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. How long do I have to return an item?"
          maxLength={1000}
        />
        <button type="submit" className="primary" disabled={busy || !query.trim()}>
          {busy ? "Searching…" : "Test"}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {result && (
        <>
          <p className="hint">
            {result.hits.length} passage{result.hits.length === 1 ? "" : "s"} ·{" "}
            {result.cache_hit ? "a chat for this exact question would be served from cache" : "not cached"}
          </p>
          {result.hits.length === 0 && (
            <p className="error">
              Nothing relevant found. The assistant would say it can&apos;t find an answer. Consider
              adding a document that covers this.
            </p>
          )}
          <ol className="hits">
            {result.hits.map((hit, i) => (
              <li key={`${hit.document_id}-${i}`} className="card">
                <div className="hit-head">
                  <strong>{hit.title}</strong>
                  <span className={`badge ${hit.dense_rank ? "doc-ready" : ""}`}>
                    {hit.dense_rank
                      ? `meaning #${hit.dense_rank} (${(hit.dense_score ?? 0).toFixed(2)})`
                      : "meaning: no match"}
                  </span>
                  <span className={`badge ${hit.sparse_rank ? "doc-ready" : ""}`}>
                    {hit.sparse_rank ? `keywords #${hit.sparse_rank}` : "keywords: no match"}
                  </span>
                  {hit.rerank_score !== null && <span className="badge doc-ready">AI score {hit.rerank_score}/10</span>}
                  <small>score {hit.score.toFixed(3)}</small>
                </div>
                {hit.section && <small className="hint">{hit.section}</small>}
                <pre>{hit.content}</pre>
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}
