import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  createConnection,
  deleteConnection,
  listConnections,
  patchConnection,
} from "../api/client";
import type { Connection } from "../types";
import { formatDate } from "./format";

type AuthType = Connection["auth_type"];

const AUTH_LABELS: Record<AuthType, string> = {
  none: "No authentication",
  bearer: "Bearer token (Authorization: Bearer …)",
  header: "API key in a custom header",
  basic: "Basic (username:password)",
};

export function ConnectionsPage() {
  const [items, setItems] = useState<Connection[] | null>(null);
  const [editing, setEditing] = useState<Connection | "new" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await listConnections());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load connections");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function remove(c: Connection) {
    if (!window.confirm(`Delete the connection “${c.name}”? Its stored credential is erased.`)) return;
    try {
      await deleteConnection(c.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not delete the connection");
    }
  }

  return (
    <div>
      <div className="toolbar">
        <p className="hint" style={{ flex: 1, margin: 0 }}>
          A connection says where one of your systems lives and how to sign in to it (for example your
          orders API or your help desk). Actions use connections to do things for your customers.
          Credentials are stored encrypted and can&apos;t be read back.
        </p>
        <button className="primary" onClick={() => setEditing(editing === "new" ? null : "new")}>
          {editing === "new" ? "Close" : "Add connection"}
        </button>
      </div>

      {editing && (
        <ConnectionForm
          key={editing === "new" ? "new" : editing.id}
          existing={editing === "new" ? null : editing}
          onDone={() => {
            setEditing(null);
            void load();
          }}
          onCancel={() => setEditing(null)}
        />
      )}
      {error && <p className="error">{error}</p>}

      {items === null ? (
        <p>Loading…</p>
      ) : items.length === 0 ? (
        <p className="hint">No connections yet. Add one to let an agent call your systems.</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Address</th>
              <th>Sign-in</th>
              <th>Used by</th>
              <th>Added</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((c) => (
              <tr key={c.id} className="static">
                <td>
                  <strong>{c.name}</strong>
                </td>
                <td>
                  <code>{c.base_url}</code>
                </td>
                <td>
                  {c.auth_type === "none" ? "None" : c.auth_type}
                  {c.secret_hint && <small> {c.secret_hint}</small>}
                </td>
                <td>{c.used_by.length > 0 ? c.used_by.join(", ") : <small>Nothing yet</small>}</td>
                <td>{formatDate(c.created_at)}</td>
                <td className="row-actions">
                  <button onClick={() => setEditing(c)}>Edit</button>
                  <button className="danger" onClick={() => void remove(c)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ConnectionForm({
  existing,
  onDone,
  onCancel,
}: {
  existing: Connection | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(existing?.name ?? "");
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? "https://");
  const [authType, setAuthType] = useState<AuthType>(existing?.auth_type ?? "bearer");
  const [headerName, setHeaderName] = useState(existing?.auth_header_name ?? "X-Api-Key");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (existing) {
        await patchConnection(existing.id, {
          name: name.trim(),
          base_url: baseUrl.trim(),
          auth_type: authType,
          auth_header_name: authType === "header" ? headerName.trim() : null,
          ...(secret ? { secret } : {}),
        });
      } else {
        await createConnection({
          name: name.trim(),
          base_url: baseUrl.trim(),
          auth_type: authType,
          auth_header_name: authType === "header" ? headerName.trim() : null,
          secret: authType === "none" ? null : secret,
        });
      }
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the connection");
      setBusy(false);
    }
  }

  const needsSecret = authType !== "none";
  const secretRequired = needsSecret && (!existing || existing.auth_type !== authType);

  return (
    <form className="card" onSubmit={onSubmit}>
      <h3>{existing ? `Edit “${existing.name}”` : "New connection"}</h3>
      <div className="user-form">
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required />
        </label>
        <label style={{ flex: 1, minWidth: "16rem" }}>
          Base address (https)
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} maxLength={500} required />
        </label>
      </div>
      <div className="user-form">
        <label>
          How do we sign in?
          <select value={authType} onChange={(e) => setAuthType(e.target.value as AuthType)}>
            {(Object.keys(AUTH_LABELS) as AuthType[]).map((t) => (
              <option key={t} value={t}>
                {AUTH_LABELS[t]}
              </option>
            ))}
          </select>
        </label>
        {authType === "header" && (
          <label>
            Header name
            <input value={headerName} onChange={(e) => setHeaderName(e.target.value)} maxLength={60} required />
          </label>
        )}
        {needsSecret && (
          <label style={{ flex: 1, minWidth: "14rem" }}>
            {authType === "basic" ? "Username:password" : "Secret"}
            <input
              type="password"
              autoComplete="new-password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              required={secretRequired}
              placeholder={existing?.has_secret && !secretRequired ? `Stored ${existing.secret_hint ?? ""}. Leave blank to keep` : ""}
            />
          </label>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy || !name.trim()}>
          {busy ? "Saving…" : "Save connection"}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
