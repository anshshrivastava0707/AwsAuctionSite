"use client";

import { useSyncExternalStore } from "react";

// Saved auctions live in this browser only until the backend grows a favorites
// table. Every heart on the page shares one store, and other tabs stay in sync
// through the `storage` event.

const KEY = "auction.favorites";
const listeners = new Set<() => void>();
let cache: string | null | undefined;
let ids: ReadonlySet<string> = new Set();
const EMPTY: ReadonlySet<string> = new Set();

function read(): ReadonlySet<string> {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(KEY);
  } catch {
    /* storage unavailable — favorites just won't persist */
  }
  if (raw !== cache) {
    cache = raw;
    try {
      ids = new Set(JSON.parse(raw ?? "[]") as string[]);
    } catch {
      ids = new Set();
    }
  }
  return ids;
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  const onStorage = (e: StorageEvent) => e.key === KEY && fn();
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(fn);
    window.removeEventListener("storage", onStorage);
  };
}

export function toggleFavorite(id: string) {
  const next = new Set(read());
  if (next.has(id)) next.delete(id);
  else next.add(id);
  try {
    localStorage.setItem(KEY, JSON.stringify([...next]));
  } catch {
    ids = next; // keep it for this page view at least
  }
  listeners.forEach((fn) => fn());
}

export function useFavorites(): ReadonlySet<string> {
  return useSyncExternalStore(subscribe, read, () => EMPTY);
}
