"use client";

import { useState } from "react";
import { listAuctions, type AuctionQuery } from "@/lib/api";
import type { Auction } from "@/lib/types";
import AuctionGrid from "./AuctionGrid";

/** First page rendered on the server; further pages fetched here as you ask for them. */
export default function AuctionList({ initial, nextCursor, query }: {
  initial: Auction[];
  nextCursor: string | null;
  query: AuctionQuery;
}) {
  const [extra, setExtra] = useState<Auction[]>([]);
  const [cursor, setCursor] = useState(nextCursor);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function more() {
    setBusy(true);
    setError(null);
    try {
      const page = await listAuctions({ ...query, cursor });
      setExtra((prev) => {
        const seen = new Set([...initial, ...prev].map((a) => a.auctionId));
        return [...prev, ...page.auctions.filter((a) => !seen.has(a.auctionId))];
      });
      setCursor(page.nextCursor);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load more");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <AuctionGrid auctions={extra.length ? [...initial, ...extra] : initial} />
      {(cursor || error) && (
        <div className="load-more">
          {error && <p className="notice bad">{error}</p>}
          {cursor && <button className="secondary" onClick={more} disabled={busy}>{busy ? "Loading…" : "Load more"}</button>}
        </div>
      )}
    </>
  );
}
