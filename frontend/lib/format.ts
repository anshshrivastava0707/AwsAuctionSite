import type { Auction, Phase } from "./types";

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export const formatCents = (cents: number | null | undefined) =>
  cents == null ? "—" : usd.format(cents / 100);

/** "12.34" -> 1234. Returns null for anything that isn't a valid non-negative amount. */
export function parseDollarsToCents(input: string): number | null {
  const trimmed = input.trim();
  if (!/^\d+(\.\d{0,2})?$/.test(trimmed)) return null;
  const [whole, frac = ""] = trimmed.split(".");
  return Number(whole) * 100 + Number(frac.padEnd(2, "0"));
}

export const centsToDollarString = (cents: number) => (cents / 100).toFixed(2);

export function formatRemaining(ms: number): string {
  if (ms <= 0) return "Ended";
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (d > 0) return `${d}d ${h}h ${pad(m)}m`;
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

export const formatDateTime = (ms: number) =>
  new Date(ms).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

/** epoch ms <-> the value of an <input type="datetime-local"> (browser's local time zone). */
export function toLocalInput(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fromLocalInput(value: string): number | null {
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? ms : null;
}

/** Same rule as backend models.phase, re-evaluated against the browser clock so
 *  "starts in" / "ended" flip on time without waiting for a server message. */
export function livePhase(a: Auction, now: number | null): Phase {
  if (a.status === "CANCELLED") return "CANCELLED";
  if (now == null) return a.phase; // before mount: trust the server (no hydration mismatch)
  if (a.status === "CLOSED" || now >= a.endsAt) return "ENDED";
  if (now < a.startsAt) return "SCHEDULED";
  return "LIVE";
}

export const PHASE_LABEL: Record<Phase, string> = {
  SCHEDULED: "Upcoming",
  LIVE: "Live",
  ENDED: "Ended",
  CANCELLED: "Cancelled",
};

export const PHASE_PILL: Record<Phase, string> = {
  SCHEDULED: "connecting",
  LIVE: "live",
  ENDED: "closed",
  CANCELLED: "closed",
};
