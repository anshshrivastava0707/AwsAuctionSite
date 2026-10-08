"use client";

/**
 * Session state for the whole app.
 *
 * Two login sources, matching the backend's AUTH_MODE:
 *  - Cognito (when NEXT_PUBLIC_COGNITO_CLIENT_ID is set): email + password. The ID
 *    token lasts an hour and is refreshed in the background from the 30-day
 *    refresh token, so people stay signed in.
 *  - dev: the backend's passwordless test login.
 * Everything else just needs "a bearer token" and doesn't care which.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, devLogin, getMe, setUnauthorizedHandler } from "./api";
import { cognitoEnabled, refreshTokens, revoke, signIn } from "./cognito";
import type { PrivateUser } from "./types";

const SESSION_KEY = "auction.session";

type Status = "loading" | "anonymous" | "onboarding" | "ready";

interface Session {
  token: string;
  expiresAt: number;
  refreshToken?: string;
}

/** Refresh this long before the ID token expires. */
const REFRESH_EARLY_MS = 5 * 60 * 1000;

interface AuthValue {
  status: Status;
  token: string | null;
  user: PrivateUser | null;
  /** The password is only used (and required) with Cognito. */
  login: (email: string, password?: string) => Promise<void>;
  logout: () => void;
  refresh: () => Promise<void>;
  setUser: (user: PrivateUser) => void;
}

const AuthContext = createContext<AuthValue | null>(null);

function readSession(): Session | null {
  try {
    const s = JSON.parse(localStorage.getItem(SESSION_KEY) ?? "null") as Session | null;
    // An expired session is still useful if it can be refreshed.
    return s && (s.expiresAt > Date.now() || s.refreshToken) ? s : null;
  } catch {
    return null;
  }
}

/** A session whose ID token is good for a while yet, refreshing it if needed. */
async function freshSession(s: Session | null): Promise<Session | null> {
  if (!s) return null;
  if (s.expiresAt - Date.now() > REFRESH_EARLY_MS) return s;
  if (!s.refreshToken || !cognitoEnabled) return s.expiresAt > Date.now() ? s : null;
  try {
    const t = await refreshTokens(s.refreshToken);
    const next = { token: t.idToken, expiresAt: t.expiresAt, refreshToken: t.refreshToken };
    writeSession(next);
    return next;
  } catch {
    return s.expiresAt > Date.now() ? s : null; // refresh token revoked or expired: sign in again
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
    const refreshToken = readSession()?.refreshToken;
    if (refreshToken && cognitoEnabled) void revoke(refreshToken);
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

  // Keep the token fresh: once at startup, shortly before it expires, and when a
  // sleeping tab wakes up (timers don't run while it sleeps).
  const tokenRef = useRef<string | null>(null);
  tokenRef.current = token;
  const syncSession = useCallback(async () => {
    const s = await freshSession(readSession());
    if ((s?.token ?? null) !== tokenRef.current) await load(s?.token ?? null);
  }, [load]);

  useEffect(() => {
    void syncSession();
    // Keep tabs in sync: logging in/out in one tab applies to all of them.
    const onStorage = (e: StorageEvent) => e.key === SESSION_KEY && void syncSession();
    const onVisible = () => document.visibilityState === "visible" && void syncSession();
    window.addEventListener("storage", onStorage);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("storage", onStorage);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [syncSession]);

  useEffect(() => {
    if (!token) return;
    const s = readSession();
    if (!s?.refreshToken) return;
    const t = setTimeout(() => void syncSession(), Math.max(s.expiresAt - Date.now() - REFRESH_EARLY_MS, 5_000));
    return () => clearTimeout(t);
  }, [token, syncSession]);

  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  const login = useCallback(
    async (email: string, password?: string) => {
      let s: Session;
      if (cognitoEnabled) {
        const t = await signIn(email.trim(), password ?? "");
        s = { token: t.idToken, expiresAt: t.expiresAt, refreshToken: t.refreshToken };
      } else {
        const d = await devLogin(email);
        s = { token: d.token, expiresAt: d.expiresAt };
      }
      writeSession(s);
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
