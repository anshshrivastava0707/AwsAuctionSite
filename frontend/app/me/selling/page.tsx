"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import RequireAccount from "@/components/RequireAccount";
import { cancelAuction, getMyAuctions } from "@/lib/api";
import { formatCents, formatDateTime, PHASE_LABEL, PHASE_PILL } from "@/lib/format";
import type { Auction } from "@/lib/types";

export default function MySellingPage() {
  return <RequireAccount role="seller">{(_user, token) => <MySelling token={token} />}</RequireAccount>;
}

function MySelling({ token }: { token: string }) {
  const [auctions, setAuctions] = useState<Auction[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    getMyAuctions(token).then(setAuctions, (e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [token]);
  useEffect(load, [load]);

  async function onCancel(a: Auction) {
    if (!window.confirm(`Cancel "${a.title}"? This can't be undone.`)) return;
    try {
      await cancelAuction(token, a.auctionId);
      load();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "Could not cancel");
    }
  }

  return (
    <div className="card">
      <div className="page-head">
        <h1>Items I&apos;m selling</h1>
        <Link href="/sell" className="button">List an item</Link>
      </div>
      {error && <p className="notice bad">{error}</p>}
      {auctions == null && !error && <p className="muted">Loading…</p>}
      {auctions?.length === 0 && <p className="muted">You haven&apos;t listed anything yet.</p>}
      {auctions && auctions.length > 0 && (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr><th>Item</th><th>Status</th><th>High bid</th><th>Bids</th><th>Starts</th><th>Ends</th><th /></tr>
            </thead>
            <tbody>
              {auctions.map((a) => {
                const editable = a.bidCount === 0 && (a.phase === "LIVE" || a.phase === "SCHEDULED");
                return (
                  <tr key={a.auctionId}>
                    <td><Link href={`/auctions/${a.auctionId}`}>{a.title}</Link></td>
                    <td><span className={`pill ${PHASE_PILL[a.phase]}`}>{PHASE_LABEL[a.phase]}</span></td>
                    <td className="num">{a.currentHigh == null ? "—" : formatCents(a.currentHigh)}</td>
                    <td className="num">{a.bidCount}</td>
                    <td className="small">{formatDateTime(a.startsAt)}</td>
                    <td className="small">{formatDateTime(a.endsAt)}</td>
                    <td>
                      {editable && (
                        <span className="actions">
                          <Link href={`/auctions/${a.auctionId}/edit`} className="button secondary small">Edit</Link>
                          <button className="danger small" onClick={() => onCancel(a)}>Cancel</button>
                        </span>
                      )}
                    </td>
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
