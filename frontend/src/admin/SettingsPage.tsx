import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { clearLlmKey, getLlmKey, setLlmKey } from "../api/client";
import type { LlmKeyStatus } from "../types";

export function SettingsPage() {
  const [status, setStatus] = useState<LlmKeyStatus | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await getLlmKey());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load settings");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setStatus(await setLlmKey(key.trim()));
      setKey("");
      setNotice("Saved. Your agents now use your own AI key.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save the key");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm("Go back to the platform's shared AI key?")) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setStatus(await clearLlmKey());
      setNotice("Removed. The platform's shared key is used again.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not remove the key");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Your own AI provider key</h3>
        <p className="hint">
          By default your agents use the platform&apos;s shared AI key. If you add your own, the usage is billed to
          your own account with the provider. It must be a key for the same provider and models the platform is
          set up with. It is checked before it is saved, stored encrypted, and can never be shown again. Used
          for conversations, document indexing, tests and quality checks.
        </p>
        {status === null ? (
          <p>Loading…</p>
        ) : (
          <p>
            {status.configured ? (
              <>
                <span className="badge doc-ready">Using your key</span> <small>{status.hint}</small>
              </>
            ) : (
              <span className="badge">Using the platform&apos;s key</span>
            )}
          </p>
        )}
        <form className="user-form" onSubmit={save}>
          <label style={{ flex: 1, minWidth: "18rem" }}>
            {status?.configured ? "Replace with a new key" : "Your API key"}
            <input
              type="password"
              autoComplete="new-password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              maxLength={500}
            />
          </label>
          <button type="submit" className="primary" disabled={busy || key.trim().length < 8}>
            {busy ? "Checking…" : "Save key"}
          </button>
          {status?.configured && (
            <button type="button" className="danger" disabled={busy} onClick={() => void remove()}>
              Remove
            </button>
          )}
        </form>
        {error && <p className="error">{error}</p>}
        {notice && <p className="notice">{notice}</p>}
      </div>
    </div>
  );
}
