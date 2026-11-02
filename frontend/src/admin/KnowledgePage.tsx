import { useCallback, useEffect, useState } from "react";

import { listCollections, listDocuments } from "../api/client";
import type { Collection, DocumentItem, DocumentStatus } from "../types";
import { CollectionsPanel } from "./CollectionsPanel";
import { QualityPanel } from "./QualityPanel";
import { DocumentDetailView } from "./DocumentDetailView";
import { formatBytes, formatDate } from "./format";
import { RetrievalTester } from "./RetrievalTester";
import { StatusBadge } from "./StatusBadge";
import { UploadPanel } from "./UploadPanel";

const PAGE_SIZE = 25;

type Section = "documents" | "collections" | "quality" | "tester";

export function KnowledgePage({
  seedTitle,
  onSeedUsed,
}: {
  seedTitle?: string | null;
  onSeedUsed?: () => void;
}) {
  const [section, setSection] = useState<Section>("documents");
  const [openId, setOpenId] = useState<string | null>(null);
  const [showUpload, setShowUpload] = useState(Boolean(seedTitle));
  const [seed] = useState(seedTitle ?? ""); // captured once so the form keeps it after the parent clears it

  useEffect(() => {
    if (seedTitle) onSeedUsed?.();
  }, [seedTitle, onSeedUsed]);

  const [collections, setCollections] = useState<Collection[]>([]);
  const [items, setItems] = useState<DocumentItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<DocumentStatus | "">("");
  const [collectionId, setCollectionId] = useState("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const loadCollections = useCallback(async () => {
    try {
      setCollections(await listCollections());
    } catch {
      /* the document list still works without the filter */
    }
  }, []);

  useEffect(() => {
    void loadCollections();
  }, [loadCollections, section]);

  // Debounce the search box so we do not query on every keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery(search.trim());
      setOffset(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const load = useCallback(async () => {
    try {
      const result = await listDocuments({ q: query, status, collectionId, limit: PAGE_SIZE, offset });
      setItems(result.items);
      setTotal(result.total);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load documents");
    }
  }, [query, status, collectionId, offset]);

  useEffect(() => {
    if (openId === null && section === "documents") void load();
  }, [load, openId, section]);

  // Keep the table live while anything is still being indexed.
  const inFlight = items?.some((d) => d.status === "pending" || d.status === "processing") ?? false;
  useEffect(() => {
    if (!inFlight || openId !== null || section !== "documents") return;
    const timer = setInterval(() => void load(), 2500);
    return () => clearInterval(timer);
  }, [inFlight, load, openId, section]);

  if (openId !== null) {
    return <DocumentDetailView id={openId} collections={collections} onBack={() => setOpenId(null)} />;
  }

  return (
    <div className="knowledge">
      <div className="subtabs">
        <button className={section === "documents" ? "active" : ""} onClick={() => setSection("documents")}>
          Documents
        </button>
        <button className={section === "collections" ? "active" : ""} onClick={() => setSection("collections")}>
          Collections
        </button>
        <button className={section === "quality" ? "active" : ""} onClick={() => setSection("quality")}>
          Quality
        </button>
        <button className={section === "tester" ? "active" : ""} onClick={() => setSection("tester")}>
          Retrieval tester
        </button>
      </div>

      {section === "tester" && <RetrievalTester />}
      {section === "quality" && <QualityPanel collections={collections} />}
      {section === "collections" && <CollectionsPanel onChanged={() => void loadCollections()} />}

      {section === "documents" && (
        <>
          <div className="toolbar">
            <input
              type="search"
              placeholder="Search by title or file name"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <select
              value={collectionId}
              onChange={(e) => {
                setCollectionId(e.target.value);
                setOffset(0);
              }}
              aria-label="Filter by collection"
            >
              <option value="">All collections</option>
              {collections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            <select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value as DocumentStatus | "");
                setOffset(0);
              }}
              aria-label="Filter by status"
            >
              <option value="">All statuses</option>
              <option value="ready">Ready</option>
              <option value="pending">Queued</option>
              <option value="processing">Processing</option>
              <option value="failed">Failed</option>
            </select>
            <button className="primary" onClick={() => setShowUpload((v) => !v)}>
              {showUpload ? "Close" : "Add documents"}
            </button>
          </div>

          {showUpload && (
            <UploadPanel
              collections={collections}
              onChanged={() => {
                void load();
                void loadCollections();
              }}
              initialMode={seed ? "paste" : "files"}
              initialTitle={seed}
            />
          )}
          {error && <p className="error">{error}</p>}

          {items === null ? (
            <p>Loading…</p>
          ) : items.length === 0 ? (
            <p className="hint">
              {query || status || collectionId
                ? "No documents match these filters."
                : "No documents yet. Add some so your agents have something to answer from."}
            </p>
          ) : (
            <table className="doc-table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Collection</th>
                  <th>Status</th>
                  <th>Chunks</th>
                  <th>Size</th>
                  <th>Added</th>
                </tr>
              </thead>
              <tbody>
                {items.map((doc) => (
                  <tr
                    key={doc.id}
                    onClick={() => setOpenId(doc.id)}
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") setOpenId(doc.id);
                    }}
                  >
                    <td>
                      <strong>{doc.title}</strong>
                      {doc.file_name && <small> {doc.file_name}</small>}
                      {doc.status === "failed" && doc.error && <div className="error small">{doc.error}</div>}
                    </td>
                    <td>{doc.collection_name ?? <small>—</small>}</td>
                    <td>
                      <StatusBadge status={doc.status} />
                    </td>
                    <td>{doc.chunk_count}</td>
                    <td>{formatBytes(doc.size_bytes)}</td>
                    <td>
                      {formatDate(doc.created_at)}
                      {doc.created_by_email && <small> {doc.created_by_email}</small>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {total > PAGE_SIZE && (
            <div className="pager">
              <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                Previous
              </button>
              <span>
                {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
              </span>
              <button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
