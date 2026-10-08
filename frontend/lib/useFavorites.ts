"use client";

import { useEffect, useSyncExternalStore } from "react";
import { getSaved, setSaved } from "./api";
import { useAuth } from "./auth";

// The signed-in user's saved auctions, shared by every heart on the page. Loaded
// once per login; toggles update it immediately and roll back if the server says no.

const LEGACY_KEY = "auction.favorites"; // hearts saved in this browser before accounts had them
const EMPTY: ReadonlySet<string> = new Set();
const listeners = new Set<() => void>();
let ids: ReadonlySet<string> = EMPTY;
let loadedFor: string | null = null;

function set(next: ReadonlySet<string>) {
  ids = next;
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function takeLegacy(): string[] {
  try {
    const raw = localStorage.getItem(LEGACY_KEY);
    localStorage.removeItem(LEGACY_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

async function load(token: string, userId: string) {
  loadedFor = userId;
  const legacy = takeLegacy();
  await Promise.all(legacy.map((id) => setSaved(token, id, true).catch(() => undefined)));
  try {
    const auctions = await getSaved(token);
    if (loadedFor === userId) set(new Set(auctions.map((a) => a.auctionId)));
  } catch {
    loadedFor = null; // try again next time
  }
}

export function useFavorites(): ReadonlySet<string> {
  const { status, token, user } = useAuth();
  const userId = status === "ready" ? user?.userId ?? null : null;
  useEffect(() => {
    if (userId && token && loadedFor !== userId) void load(token, userId);
    if (!userId && status !== "loading" && loadedFor !== null) {
      loadedFor = null;
      set(EMPTY);
    }
  }, [userId, token, status]);
  return useSyncExternalStore(subscribe, () => ids, () => EMPTY);
}

export async function toggleFavorite(token: string, auctionId: string): Promise<void> {
  const before = ids;
  const next = new Set(before);
  const saving = !next.has(auctionId);
  if (saving) next.add(auctionId);
  else next.delete(auctionId);
  set(next);
  try {
    await setSaved(token, auctionId, saving);
  } catch {
    set(before);
  }
}
