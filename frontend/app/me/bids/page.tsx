"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import RequireAccount from "@/components/RequireAccount";
import { getMyBids } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatCents, formatDateTime, formatRemaining, livePhase, PHASE_LABEL, PHASE_PILL } from "@/lib/format";
import type { Auction, BidHistoryEntry, PrivateUser } from "@/lib/types";
import { useNow } from "@/lib/useNow";
import { type AlertKind, useWatchAuctions } from "@/lib/useWatchAuctions";

const CONN_LABEL = { connecting: "Connecting…", live: "Connected", reconnecting: "Reconnecting…", unconfigured: "Not configured" };

const ALERT_TEXT: Record<AlertKind, (title: string, amount: string) => string> = {
  OUTBID: (t, amt) => `You were outbid on "${t}" — the high bid is now ${amt}.`,
  WON: (t) => `You won "${t}"!`,
  LOST: (t) => `"${t}" has ended — someone else won.`,
  RESERVE_NOT_MET: (t) => `"${t}" ended below the seller's reserve, so it didn't sell.`,
  CANCELLED: (t) => `"${t}" was cancelled.`,
};
const ALERT_TONE: Record<AlertKind, string> = {
  OUTBID: "bad", WON: "ok", LOST: "warn", RESERVE_NOT_MET: "warn", CANCELLED: "warn",
};

export default function MyBidsPage() {
  return <RequireAccount>{(user, token) => <MyBids user={user} token={token} />}</RequireAccount>;
}

function outcome(auction: Auction, userId: string, now: number | null): { text: string; tone: string } {
  const leading = auction.highBidderId === userId;
  const phase = livePhase(auction, now);
  if (phase === "CANCELLED") return { text: auction.removed ? "Removed" : "Cancelled", tone: "" };
  if (phase === "ENDED") {
    if (!auction.reserveMet) return { text: "Reserve not met", tone: "warn" };
    return leading ? { text: "Won", tone: "ok" } : { text: "Lost", tone: "bad" };
  }
  return leading ? { text: "Winning", tone: "ok" } : { text: "Outbid", tone: "bad" };
}

function MyBids({ user, token }: { user: PrivateUser; token: string }) {
  const { status } = useAuth();
  const [entries, setEntries] = useState<BidHistoryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const now = useNow();

  useEffect(() => {
    getMyBids(token).then(setEntries, (e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [token]);

  const initial = useMemo(() => entries?.map((e) => e.auction) ?? [], [entries]);
  const { auctions, conn, alerts, dismiss } = useWatchAuctions(initial, user.userId, token, status === "ready" && entries != null);

  // Unread alerts show in the tab title, so an outbid is noticed from another tab.
  useEffect(() => {
    const base = "My bids · BidBloom";
    document.title = alerts.length ? `(${alerts.length}) ${alerts[0].kind === "OUTBID" ? "Outbid!" : "Update"} · ${base}` : base;
    return () => { document.title = "BidBloom"; };
  }, [alerts]);

  const flagged = new Set(alerts.map((a) => a.auctionId));

  return (
    <div className="card">
      <div className="page-head">
        <h1>My bids</h1>
        {entries && entries.length > 0 && (
          <span className={`pill ${conn}`} title="Real-time connection"><span className="dot" /> {CONN_LABEL[conn]}</span>
        )}
      </div>

      {alerts.length > 0 && (
        <div className="stack" style={{ marginBottom: 14 }}>
          {alerts.map((a) => (
            <div key={a.id} className={`notice ${ALERT_TONE[a.kind]} alert-row`}>
              <span>
                {ALERT_TEXT[a.kind](a.title, formatCents(a.amount))}{" "}
                {a.kind === "OUTBID" && <Link href={`/auctions/${a.auctionId}`}>Bid again →</Link>}
              </span>
              <button className="secondary small" onClick={() => dismiss(a.id)} aria-label="Dismiss">Dismiss</button>
            </div>
          ))}
        </div>
      )}

      <p className="hint">
        Updates live while this page is open. Only accepted bids are listed; a bid placed a moment ago can take a
        second to show up after a reload.
      </p>
      {error && <p className="notice bad">{error}</p>}
      {entries == null && !error && <p className="muted">Loading…</p>}
      {entries?.length === 0 && <p className="muted">You haven&apos;t bid on anything yet. <Link href="/">Browse auctions</Link></p>}
      {entries && entries.length > 0 && (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr><th>Auction</th><th>Your highest bid</th><th>Your maximum</th><th>Current high</th><th>Time left</th><th>Your bids</th><th>Result</th></tr>
            </thead>
            <tbody>
              {entries.map((e) => {
                const a = auctions[e.auction.auctionId] ?? e.auction;
                const phase = livePhase(a, now);
                const mine = Math.max(...e.bids.map((b) => b.amount), a.highBidderId === user.userId ? a.currentHigh ?? 0 : 0);
                const o = outcome(a, user.userId, now);
                return (
                  <tr key={a.auctionId} className={flagged.has(a.auctionId) ? "flash" : undefined}>
                    <td>
                      <Link href={`/auctions/${a.auctionId}`}>{a.title}</Link>{" "}
                      <span className={`pill ${PHASE_PILL[phase]}`}>{PHASE_LABEL[phase]}</span>
                    </td>
                    <td className="num">{formatCents(mine)}</td>
                    <td className="num" title="Automatic bidding goes up to this. Only you can see it.">
                      {e.myMax != null && e.myMax > mine ? formatCents(e.myMax) : "—"}
                    </td>
                    <td className="num">
                      {formatCents(a.currentHigh)}
                      {a.highBidderId && a.highBidderId !== user.userId && <span className="muted small"> by {a.highBidderName}</span>}
                    </td>
                    <td className="num small">
                      {phase === "LIVE" && now != null ? formatRemaining(a.endsAt - now)
                        : phase === "SCHEDULED" ? `starts ${formatDateTime(a.startsAt)}` : "—"}
                    </td>
                    <td className="num">{e.bids.length}</td>
                    <td>{o.tone ? <span className={`notice ${o.tone}`} style={{ padding: "2px 8px" }}>{o.text}</span> : o.text}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
