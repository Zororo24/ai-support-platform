import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import {
  getToken,
  login as apiLogin,
  logoutEverywhere,
  me,
  refreshSession,
  setToken,
  setUnauthorizedHandler,
} from "../api/client";
import type { User } from "../types";

// Access tokens last 60 minutes; renewing every 20 keeps an open tab signed in without ever
// holding a long-lived credential. A closed laptop simply expires and asks for a password.
const REFRESH_INTERVAL_MS = 20 * 60 * 1000;

type AuthState = {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  logoutAll: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState<boolean>(getToken() !== null);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
  }, []);

  // Any rejected token (expired, revoked, deactivated) returns the person to the login page.
  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  useEffect(() => {
    if (getToken() === null) return;
    me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!user) return;
    const timer = setInterval(() => {
      refreshSession()
        .then((result) => setToken(result.access_token))
        .catch(() => {
          /* a 401 already triggered logout; transient network errors retry next tick */
        });
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [user]);

  const login = useCallback(async (email: string, password: string) => {
    const result = await apiLogin(email, password);
    setToken(result.access_token);
    setUser(result.user);
  }, []);

  const logoutAll = useCallback(async () => {
    try {
      await logoutEverywhere();
    } finally {
      logout();
    }
  }, [logout]);

  const value = useMemo(
    () => ({ user, loading, login, logout, logoutAll }),
    [user, loading, login, logout, logoutAll],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
