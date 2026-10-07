"use client";

/**
 * Session state for the whole app.
 *
 * Today the only login is the backend's AUTH_MODE=dev (email, no password). When
 * Cognito / Supabase is added, only `login`/`logout` here change: they'd run the
 * provider's sign-in flow and store its ID token. Everything else just needs "a
 * bearer token" and keeps working.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { ApiError, devLogin, getMe, setUnauthorizedHandler } from "./api";
import type { PrivateUser } from "./types";

const SESSION_KEY = "auction.session";

type Status = "loading" | "anonymous" | "onboarding" | "ready";

interface Session {
  token: string;
  expiresAt: number;
}

interface AuthValue {
  status: Status;
  token: string | null;
  user: PrivateUser | null;
  login: (email: string) => Promise<void>;
  logout: () => void;
  refresh: () => Promise<void>;
  setUser: (user: PrivateUser) => void;
}

const AuthContext = createContext<AuthValue | null>(null);

function readSession(): Session | null {
  try {
    const s = JSON.parse(localStorage.getItem(SESSION_KEY) ?? "null") as Session | null;
    return s && s.expiresAt > Date.now() ? s : null;
  } catch {
    return null;
  }
}

function writeSession(s: Session | null) {
  try {
    if (s) localStorage.setItem(SESSION_KEY, JSON.stringify(s));
    else localStorage.removeItem(SESSION_KEY);
  } catch {
    /* storage unavailable — the session just won't survive a reload */
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<Status>("loading");
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<PrivateUser | null>(null);

  const logout = useCallback(() => {
    writeSession(null);
    setToken(null);
    setUser(null);
    setStatus("anonymous");
  }, []);

  const load = useCallback(async (t: string | null) => {
    setToken(t);
    if (!t) {
      setUser(null);
      setStatus("anonymous");
      return;
    }
    try {
      setUser(await getMe(t));
      setStatus("ready");
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setUser(null);
        setStatus("onboarding"); // logged in, but no account yet
      } else if (e instanceof ApiError && e.status === 401) {
        writeSession(null);
        setToken(null);
        setStatus("anonymous");
      } else {
        setStatus("anonymous");
      }
    }
  }, []);

  useEffect(() => {
    void load(readSession()?.token ?? null);
    // Keep tabs in sync: logging in/out in one tab applies to all of them.
    const onStorage = (e: StorageEvent) => e.key === SESSION_KEY && void load(readSession()?.token ?? null);
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [load]);

  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  const login = useCallback(
    async (email: string) => {
      const s = await devLogin(email);
      writeSession({ token: s.token, expiresAt: s.expiresAt });
      await load(s.token);
    },
    [load],
  );

  const refresh = useCallback(() => load(token), [load, token]);

  const value = useMemo(
    () => ({ status, token, user, login, logout, refresh, setUser }),
    [status, token, user, login, logout, refresh],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
