import { useCallback, useEffect, useState } from "react";

import {
  addProbe,
  deleteProbe,
  generateProbes,
  getCollectionQuality,
  listDocuments,
  listProbes,
  reindexCollection,
  runCollectionQuality,
} from "../api/client";
import type { Collection, CollectionQuality, DocumentItem, QualityProbe } from "../types";
import { formatDate } from "./format";

const pct = (value: number) => `${Math.round(value * 100)}%`;

/** Does search find the right document? Test questions, a measured score, and what to fix. */
export function QualityPanel({ collections }: { collections: Collection[] }) {
  const [collectionId, setCollectionId] = useState(collections[0]?.id ?? "");
  const [quality, setQuality] = useState<CollectionQuality | null>(null);
  const [probes, setProbes] = useState<QualityProbe[]>([]);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rerank, setRerank] = useState(false);
  const [newDoc, setNewDoc] = useState("");
  const [newQuestion, setNewQuestion] = useState("");

  const load = useCallback(async () => {
    if (!collectionId) return;
    try {
      const [q, p, d] = await Promise.all([
        getCollectionQuality(collectionId),
        listProbes(collectionId),
        listDocuments({ collectionId, limit: 200 }),
      ]);
      setQuality(q);
      setProbes(p);
      setDocuments(d.items);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the quality report");
    }
  }, [collectionId]);

  useEffect(() => {
    if (!collectionId && collections[0]) setCollectionId(collections[0].id);
  }, [collections, collectionId]);

  useEffect(() => {
    setQuality(null);
    void load();
  }, [load]);

  async function act(name: string, work: () => Promise<string | void>) {
    setBusy(name);
    setError(null);
    setNotice(null);
    try {
      const message = await work();
      if (message) setNotice(message);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "That did not work");
    } finally {
      setBusy(null);
    }
  }

  if (collections.length === 0) return <p className="hint">Create a collection of documents first.</p>;

  const latest = quality?.latest ?? null;
  const previous = quality?.previous ?? null;
  const delta = latest && previous ? latest.doc_recall - previous.doc_recall : null;

  return (
    <div>
      <p className="hint">
        Quality checks ask the knowledge base the questions customers are likely to ask and measure how often
        the right document comes back near the top. Questions are written by the AI from your own documents (you
        can add your own), so you can see what a change to a document or a setting really does.
      </p>
      <label>
        Collection
        <select value={collectionId} onChange={(e) => setCollectionId(e.target.value)} style={{ maxWidth: "24rem" }}>
          {collections.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </label>
      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      <div className="card">
        <div className="doc-head">
          <h3 style={{ margin: 0 }}>Test questions ({quality?.probe_count ?? 0})</h3>
          <div className="row-actions">
            <button
              disabled={busy !== null || (quality?.document_count ?? 0) === 0}
              onClick={() =>
                void act("generate", async () => {
                  const r = await generateProbes(collectionId, 3);
                  return r.created > 0 ? `Wrote ${r.created} new questions.` : "Every document already has questions.";
                })
              }
            >
              {busy === "generate" ? "Writing…" : "Write questions with AI"}
            </button>
            <button
              disabled={busy !== null}
              title="Apply the latest indexing rules to every document. Unchanged passages are not re-processed."
              onClick={() =>
                void act("reindex", async () => {
                  const r = await reindexCollection(collectionId);
                  return `Re-indexed ${r.documents} document${r.documents === 1 ? "" : "s"}.`;
                })
              }
            >
              {busy === "reindex" ? "Re-indexing…" : "Rebuild index"}
            </button>
          </div>
        </div>
        {probes.length === 0 ? (
          <p className="hint">None yet. Write some with the AI, or add your own below.</p>
        ) : (
          <table className="doc-table">
            <tbody>
              {probes.map((p) => (
                <tr key={p.id} className="static">
                  <td>{p.question}</td>
                  <td>
                    <small>{p.document_title}</small>
                  </td>
                  <td>
                    <span className="badge">{p.source === "manual" ? "yours" : "AI"}</span>
                  </td>
                  <td className="row-actions">
                    <button className="danger" onClick={() => void act("delete", async () => void (await deleteProbe(p.id)))}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="user-form">
          <label style={{ flex: 1, minWidth: "16rem" }}>
            Add your own question
            <input value={newQuestion} onChange={(e) => setNewQuestion(e.target.value)} maxLength={300} />
          </label>
          <label>
            …which this document should answer
            <select value={newDoc} onChange={(e) => setNewDoc(e.target.value)}>
              <option value="">Choose…</option>
              {documents.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.title}
                </option>
              ))}
            </select>
          </label>
          <button
            disabled={busy !== null || !newDoc || newQuestion.trim().length < 5}
            onClick={() =>
              void act("add", async () => {
                await addProbe(collectionId, newDoc, newQuestion.trim());
                setNewQuestion("");
              })
            }
          >
            Add
          </button>
        </div>
      </div>

      <div className="card">
        <div className="doc-head">
          <h3 style={{ margin: 0 }}>How well does search find the answer?</h3>
          <div className="row-actions">
            <label className="inline">
              <input type="checkbox" checked={rerank} onChange={(e) => setRerank(e.target.checked)} /> with AI
              double-check
            </label>
            <button
              className="primary"
              disabled={busy !== null || (quality?.probe_count ?? 0) === 0}
              onClick={() => void act("report", async () => void (await runCollectionQuality(collectionId, { rerank })))}
            >
              {busy === "report" ? "Measuring…" : "Run quality check"}
            </button>
          </div>
        </div>
        {latest === null ? (
          <p className="hint">Not measured yet.</p>
        ) : (
          <>
            <p>
              <strong style={{ fontSize: "1.6rem" }}>{pct(latest.doc_recall)}</strong> of {latest.probe_count} questions
              found the right document in the top {latest.k}
              {latest.rerank ? " (with AI double-check)" : ""}.{" "}
              {delta !== null && delta !== 0 && (
                <span className={delta > 0 ? "" : "error"}>
                  {delta > 0 ? "▲" : "▼"} {Math.abs(Math.round(delta * 100))} points since the last check.
                </span>
              )}
            </p>
            <p className="hint">
              The exact passage was found {pct(latest.chunk_recall)} of the time · average position score{" "}
              {latest.mrr.toFixed(2)} (1.00 = always first) · measured {formatDate(latest.created_at)}
            </p>

            {latest.issues.length > 0 && (
              <>
                <h4>To look at</h4>
                <ul>
                  {latest.issues.map((i, n) => (
                    <li key={n}>
                      <strong>{i.title}</strong>: {i.detail}
                    </li>
                  ))}
                </ul>
              </>
            )}
            {latest.misses.length > 0 && (
              <>
                <h4>Questions that missed</h4>
                <table className="doc-table">
                  <thead>
                    <tr>
                      <th>Question</th>
                      <th>Should find</th>
                      <th>Found instead</th>
                    </tr>
                  </thead>
                  <tbody>
                    {latest.misses.map((m, n) => (
                      <tr key={n} className="static">
                        <td>{m.question}</td>
                        <td>{m.expected}</td>
                        <td>{m.got.length ? m.got.join(", ") : <small>nothing</small>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
            <h4>By document</h4>
            <table className="doc-table">
              <tbody>
                {latest.per_document.map((d) => (
                  <tr key={d.document_id} className="static">
                    <td>{d.title}</td>
                    <td>
                      {d.hits} of {d.probes}
                    </td>
                    <td>
                      <span className={`badge ${d.recall >= 0.8 ? "doc-ready" : d.recall >= 0.5 ? "doc-pending" : "doc-failed"}`}>
                        {pct(d.recall)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}
