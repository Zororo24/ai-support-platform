import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { createCollection, deleteCollection, listCollections, patchCollection } from "../api/client";
import type { Collection } from "../types";

export function CollectionsPanel({ onChanged }: { onChanged: () => void }) {
  const [items, setItems] = useState<Collection[] | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setItems(await listCollections());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load collections");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await load();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  function onCreate(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      await createCollection(name.trim(), description.trim());
      setName("");
      setDescription("");
    });
  }

  function rename(c: Collection) {
    const next = window.prompt("New name", c.name);
    if (next && next.trim() && next.trim() !== c.name) void run(() => patchCollection(c.id, { name: next.trim() }));
  }

  function remove(c: Collection) {
    if (window.confirm(`Delete the collection “${c.name}”?`)) void run(() => deleteCollection(c.id));
  }

  return (
    <div>
      <p className="hint">
        Collections group documents. An agent only answers from the collections you attach to it, so you can
        run, for example, a sales agent and a support agent with different knowledge.
      </p>
      <form className="card user-form" onSubmit={onCreate}>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required />
        </label>
        <label>
          Description (optional)
          <input value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} />
        </label>
        <button type="submit" className="primary" disabled={busy || !name.trim()}>
          Add collection
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {items === null ? (
        <p>Loading…</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Documents</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((c) => (
              <tr key={c.id} className="static">
                <td>
                  <strong>{c.name}</strong> {c.is_default && <span className="badge">Default</span>}
                  {c.description && <div className="hint">{c.description}</div>}
                </td>
                <td>{c.document_count}</td>
                <td className="row-actions">
                  <button disabled={busy} onClick={() => rename(c)}>
                    Rename
                  </button>
                  {!c.is_default && (
                    <button className="danger" disabled={busy} onClick={() => remove(c)}>
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
