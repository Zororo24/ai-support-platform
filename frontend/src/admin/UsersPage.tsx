import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { createUser, listUsers, patchUser } from "../api/client";
import type { AdminUser, Role } from "../types";
import { formatDate } from "./format";

export function UsersPage({ currentUserId }: { currentUserId: string }) {
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("customer");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    try {
      setUsers(await listUsers());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load users");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function change(
    user: AdminUser,
    changes: { role?: Role; is_active?: boolean; password?: string },
    message: string,
  ) {
    setBusyId(user.id);
    setError(null);
    setNotice(null);
    try {
      await patchUser(user.id, changes);
      setNotice(message);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Update failed");
    } finally {
      setBusyId(null);
    }
  }

  async function onCreate(event: FormEvent) {
    event.preventDefault();
    setCreating(true);
    setError(null);
    setNotice(null);
    try {
      const created = await createUser(email.trim(), password, role);
      setNotice(`Created ${created.email}. Share the password with them securely.`);
      setEmail("");
      setPassword("");
      setRole("customer");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the user");
    } finally {
      setCreating(false);
    }
  }

  function resetPassword(user: AdminUser) {
    const next = window.prompt(`New password for ${user.email} (at least 8 characters):`);
    if (next === null) return;
    if (next.length < 8) {
      setError("Passwords must be at least 8 characters");
      return;
    }
    void change(user, { password: next }, `Password updated for ${user.email}.`);
  }

  return (
    <div className="users-page">
      <form className="card" onSubmit={onCreate}>
        <h3>Add a user</h3>
        <div className="user-form">
          <label>
            Email
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </label>
          <label>
            Initial password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={8}
              required
              autoComplete="new-password"
            />
          </label>
          <label>
            Role
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="customer">Customer</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <button type="submit" className="primary" disabled={creating || !email.trim() || password.length < 8}>
            {creating ? "Adding…" : "Add user"}
          </button>
        </div>
      </form>

      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      {users === null ? (
        <p>Loading…</p>
      ) : (
        <table className="doc-table">
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              <th>Added</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const isSelf = u.id === currentUserId;
              const busy = busyId === u.id;
              return (
                <tr key={u.id} className="static">
                  <td>
                    {u.email}
                    {isSelf && <small> (you)</small>}
                  </td>
                  <td>
                    <select
                      aria-label={`Role for ${u.email}`}
                      value={u.role}
                      disabled={isSelf || busy}
                      onChange={(e) => void change(u, { role: e.target.value as Role }, `Role updated for ${u.email}.`)}
                    >
                      <option value="customer">Customer</option>
                      <option value="admin">Admin</option>
                    </select>
                  </td>
                  <td>
                    <span className={`badge ${u.is_active ? "doc-ready" : "doc-failed"}`}>
                      {u.is_active ? "Active" : "Deactivated"}
                    </span>
                  </td>
                  <td>{formatDate(u.created_at)}</td>
                  <td className="row-actions">
                    <button disabled={busy} onClick={() => resetPassword(u)}>
                      Reset password
                    </button>
                    {!isSelf && (
                      <button
                        disabled={busy}
                        className={u.is_active ? "danger" : ""}
                        onClick={() =>
                          void change(
                            u,
                            { is_active: !u.is_active },
                            `${u.email} ${u.is_active ? "deactivated" : "reactivated"}.`,
                          )
                        }
                      >
                        {u.is_active ? "Deactivate" : "Reactivate"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
