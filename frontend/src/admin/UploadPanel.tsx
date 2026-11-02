import { useRef, useState } from "react";
import type { DragEvent, FormEvent } from "react";

import { createDocument, uploadDocument } from "../api/client";
import type { Collection } from "../types";
import { ACCEPTED_EXTENSIONS, MAX_UPLOAD_BYTES, formatBytes } from "./format";

type UploadItem = {
  key: string;
  name: string;
  size: number;
  state: "uploading" | "queued" | "error";
  message?: string;
};

function validateFile(file: File): string | null {
  const lower = file.name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))) {
    return `Unsupported type. Allowed: ${ACCEPTED_EXTENSIONS.join(", ")}`;
  }
  if (file.size === 0) return "The file is empty";
  if (file.size > MAX_UPLOAD_BYTES) return `Larger than ${formatBytes(MAX_UPLOAD_BYTES)}`;
  return null;
}

type Props = {
  collections: Collection[];
  onChanged: () => void;
  initialMode?: "files" | "paste";
  initialTitle?: string;
};

export function UploadPanel({ collections, onChanged, initialMode = "files", initialTitle = "" }: Props) {
  const [collectionId, setCollectionId] = useState(""); // empty = the default collection
  const [mode, setMode] = useState<"files" | "paste">(initialMode);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const [title, setTitle] = useState(initialTitle);
  const [content, setContent] = useState("");
  const [pasteBusy, setPasteBusy] = useState(false);
  const [pasteError, setPasteError] = useState<string | null>(null);

  function patch(key: string, changes: Partial<UploadItem>) {
    setItems((prev) => prev.map((item) => (item.key === key ? { ...item, ...changes } : item)));
  }

  async function handleFiles(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      const key = `${file.name}-${file.size}-${crypto.randomUUID()}`;
      const problem = validateFile(file);
      if (problem) {
        setItems((prev) => [
          ...prev,
          { key, name: file.name, size: file.size, state: "error", message: problem },
        ]);
        continue;
      }
      setItems((prev) => [...prev, { key, name: file.name, size: file.size, state: "uploading" }]);
      try {
        await uploadDocument(file, undefined, collectionId || undefined);
        patch(key, { state: "queued" });
        onChanged();
      } catch (e) {
        patch(key, { state: "error", message: e instanceof Error ? e.message : "Upload failed" });
      }
    }
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    void handleFiles(event.dataTransfer.files);
  }

  async function onPaste(event: FormEvent) {
    event.preventDefault();
    setPasteBusy(true);
    setPasteError(null);
    try {
      await createDocument(title.trim(), content, collectionId || undefined);
      setTitle("");
      setContent("");
      onChanged();
    } catch (e) {
      setPasteError(e instanceof Error ? e.message : "Could not add the document");
    } finally {
      setPasteBusy(false);
    }
  }

  return (
    <section className="card upload-panel">
      <div className="subtabs">
        <button className={mode === "files" ? "active" : ""} onClick={() => setMode("files")}>
          Upload files
        </button>
        <button className={mode === "paste" ? "active" : ""} onClick={() => setMode("paste")}>
          Paste text
        </button>
      </div>

      <label>
        Add to collection
        <select value={collectionId} onChange={(e) => setCollectionId(e.target.value)}>
          <option value="">Default collection</option>
          {collections
            .filter((c) => !c.is_default)
            .map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
        </select>
      </label>

      {mode === "files" ? (
        <>
          <div
            className={`dropzone ${dragging ? "dragging" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => inputRef.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
            }}
          >
            <p>
              <strong>Drop files here</strong> or click to browse
            </p>
            <small>
              {ACCEPTED_EXTENSIONS.join(", ")} · up to {formatBytes(MAX_UPLOAD_BYTES)} each
            </small>
            <input
              ref={inputRef}
              type="file"
              multiple
              hidden
              accept={ACCEPTED_EXTENSIONS.join(",")}
              onChange={(e) => {
                if (e.target.files) void handleFiles(e.target.files);
                e.target.value = "";
              }}
            />
          </div>
          {items.length > 0 && (
            <ul className="upload-list">
              {items.map((item) => (
                <li key={item.key} className={item.state}>
                  <span className="name">{item.name}</span>
                  <small>{formatBytes(item.size)}</small>
                  <span className="state">
                    {item.state === "uploading" && "Uploading…"}
                    {item.state === "queued" && "Uploaded. Indexing in the background"}
                    {item.state === "error" && (item.message ?? "Failed")}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <form onSubmit={onPaste} className="paste-form">
          <label>
            Title
            <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={300} />
          </label>
          <label>
            Content (Markdown or plain text)
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={8}
              required
              maxLength={500000}
            />
          </label>
          {pasteError && <p className="error">{pasteError}</p>}
          <button type="submit" disabled={pasteBusy || !title.trim() || !content.trim()}>
            {pasteBusy ? "Adding…" : "Add to knowledge base"}
          </button>
        </form>
      )}
    </section>
  );
}
