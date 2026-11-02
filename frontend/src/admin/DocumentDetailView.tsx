import { useCallback, useEffect, useState } from "react";

import {
  deleteDocument,
  getDocument,
  patchDocument,
  reindexDocument,
} from "../api/client";
import type { Collection, DocumentDetail } from "../types";
import { formatBytes, formatDate } from "./format";
import { StatusBadge } from "./StatusBadge";

export function DocumentDetailView({
  id,
  collections,
  onBack,
}: {
  id: string;
  collections: Collection[];
  onBack: () => void;
}) {
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (resetEditor: boolean) => {
      try {
        const loaded = await getDocument(id);
        setDoc(loaded);
        if (resetEditor) {
          setTitle(loaded.title);
          setContent(loaded.content);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not load the document");
      }
    },
    [id],
  );

  useEffect(() => {
    void load(true);
  }, [load]);

  // Follow indexing progress without clobbering what the admin is typing.
  const inFlight = doc?.status === "pending" || doc?.status === "processing";
  useEffect(() => {
    if (!inFlight) return;
    const timer = setInterval(() => void load(false), 2000);
    return () => clearInterval(timer);
  }, [inFlight, load]);

  async function run(action: () => Promise<unknown>, after?: () => void) {
    setBusy(true);
    setError(null);
    try {
      await action();
      after?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  if (error && !doc) {
    return (
      <div>
        <button onClick={onBack}>← Back</button>
        <p className="error">{error}</p>
      </div>
    );
  }
  if (!doc) return <p>Loading…</p>;

  const dirty = title !== doc.title || content !== doc.content;
  const contentChanged = content !== doc.content;

  return (
    <div className="doc-detail">
      <button onClick={onBack}>← Back to documents</button>

      <div className="card">
        <div className="doc-head">
          <h2>{doc.title}</h2>
          <StatusBadge status={doc.status} />
        </div>
        <dl className="meta">
          <dt>Source</dt>
          <dd>{doc.file_name ?? doc.source}</dd>
          <dt>Size</dt>
          <dd>{formatBytes(doc.size_bytes)}</dd>
          <dt>Chunks</dt>
          <dd>{doc.chunk_count}</dd>
          <dt>Collection</dt>
          <dd>
            <select
              aria-label="Collection"
              value={doc.collection_id ?? ""}
              disabled={busy}
              onChange={(e) => void run(() => patchDocument(id, { collection_id: e.target.value || null }), () => load(false))}
            >
              {collections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </dd>
          <dt>Added</dt>
          <dd>
            {formatDate(doc.created_at)}
            {doc.created_by_email ? ` by ${doc.created_by_email}` : ""}
          </dd>
        </dl>
        {doc.status === "failed" && doc.error && (
          <p className="error">
            Indexing failed: {doc.error}{" "}
            <button disabled={busy} onClick={() => run(() => reindexDocument(id), () => load(false))}>
              Retry
            </button>
          </p>
        )}
        {error && <p className="error">{error}</p>}
      </div>

      <div className="card">
        <h3>Edit</h3>
        <label>
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} />
        </label>
        <label>
          Content
          <textarea value={content} onChange={(e) => setContent(e.target.value)} rows={12} />
        </label>
        <div className="actions">
          <button
            type="button"
            className="primary"
            disabled={busy || !dirty || !title.trim() || !content.trim()}
            onClick={() =>
              run(
                () =>
                  patchDocument(id, {
                    ...(title !== doc.title ? { title } : {}),
                    ...(contentChanged ? { content } : {}),
                  }),
                () => load(true),
              )
            }
          >
            {contentChanged ? "Save and re-index" : "Save"}
          </button>
          <button
            type="button"
            disabled={busy || inFlight}
            onClick={() => run(() => reindexDocument(id), () => load(false))}
          >
            Re-index
          </button>
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={() => {
              if (window.confirm(`Delete “${doc.title}”? The assistant will stop using it immediately.`)) {
                void run(() => deleteDocument(id), onBack);
              }
            }}
          >
            Delete
          </button>
        </div>
      </div>

      <div className="card">
        <h3>Chunks ({doc.chunks.length})</h3>
        {doc.chunks.length === 0 ? (
          <p className="hint">No chunks yet.</p>
        ) : (
          <ol className="chunks">
            {doc.chunks.map((chunk) => (
              <li key={chunk.index}>
                <small>#{chunk.index + 1}</small>
                <pre>{chunk.content}</pre>
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
