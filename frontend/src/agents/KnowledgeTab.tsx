import { useEffect, useState } from "react";

import { listCollections } from "../api/client";
import type { AgentConfig, Collection } from "../types";

type Props = {
  config: AgentConfig;
  setConfig: (config: AgentConfig) => void;
};

export function KnowledgeTab({ config, setConfig }: Props) {
  const [collections, setCollections] = useState<Collection[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listCollections()
      .then(setCollections)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load collections"));
  }, []);

  const selected = new Set(config.knowledge.collection_ids);

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setConfig({ ...config, knowledge: { ...config.knowledge, collection_ids: [...next] } });
  }

  return (
    <div className="knowledge-tab">
      <div className="card">
        <h3>What this agent knows</h3>
        <p className="hint">
          Pick the collections of documents this agent may answer from. Manage documents and collections in
          the <strong>Knowledge base</strong> section.
        </p>
        {error && <p className="error">{error}</p>}
        {collections === null ? (
          <p>Loading…</p>
        ) : (
          <ul className="check-list">
            {collections.map((c) => (
              <li key={c.id}>
                <label className="inline">
                  <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggle(c.id)} />
                  <span>
                    <strong>{c.name}</strong> <small>({c.document_count} documents)</small>
                    {c.description && <span className="hint"> {c.description}</span>}
                  </span>
                </label>
              </li>
            ))}
          </ul>
        )}
        {selected.size === 0 && (
          <p className="error">
            No collections selected: this agent will say it can&apos;t find anything. Pick at least one.
          </p>
        )}
      </div>

      <div className="card">
        <h3>Retrieval settings</h3>
        <p className="hint">Defaults work for most documents. Use the retrieval tester to check changes.</p>
        <div className="user-form">
          <label>
            Passages used per answer
            <input
              type="number"
              min={1}
              max={10}
              value={config.knowledge.top_k}
              onChange={(e) =>
                setConfig({
                  ...config,
                  knowledge: { ...config.knowledge, top_k: Number(e.target.value) || 5 },
                })
              }
            />
          </label>
          <label>
            Minimum similarity (0 to 0.9)
            <input
              type="number"
              min={0}
              max={0.9}
              step={0.05}
              value={config.knowledge.min_similarity}
              onChange={(e) =>
                setConfig({
                  ...config,
                  knowledge: { ...config.knowledge, min_similarity: Number(e.target.value) },
                })
              }
            />
          </label>
        </div>
        <label className="inline">
          <input
            type="checkbox"
            checked={config.knowledge.rewrite_queries}
            onChange={(e) =>
              setConfig({ ...config, knowledge: { ...config.knowledge, rewrite_queries: e.target.checked } })
            }
          />
          <span>
            <strong>Understand follow-up questions.</strong> Rewrite “what about gift cards?” into a complete
            search using the conversation so far. <em>One extra AI call per follow-up.</em>
          </span>
        </label>
        <label className="inline">
          <input
            type="checkbox"
            checked={config.knowledge.rerank}
            onChange={(e) => setConfig({ ...config, knowledge: { ...config.knowledge, rerank: e.target.checked } })}
          />
          <span>
            <strong>Double-check the best passages.</strong> Let the AI put the most relevant passages first
            before answering. <em>One extra AI call per search.</em>
          </span>
        </label>
        <p className="hint">
          Check whether a setting helps with <strong>Knowledge base → Quality</strong>: it measures how often
          the right document is found, with and without these options.
        </p>
      </div>
    </div>
  );
}
