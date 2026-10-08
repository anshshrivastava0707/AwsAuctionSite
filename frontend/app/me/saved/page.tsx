"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import AuctionGrid from "@/components/AuctionGrid";
import RequireAccount from "@/components/RequireAccount";
import { getSaved } from "@/lib/api";
import type { Auction } from "@/lib/types";
import { useFavorites } from "@/lib/useFavorites";

export default function SavedPage() {
  return <RequireAccount>{(_, token) => <Saved token={token} />}</RequireAccount>;
}

function Saved({ token }: { token: string }) {
  const [auctions, setAuctions] = useState<Auction[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const saved = useFavorites();

  useEffect(() => {
    getSaved(token).then(setAuctions, (e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [token]);

  // Un-hearting a card here removes it from the page straight away. Track removals
  // (saved -> not saved) rather than filtering by `saved`, which is empty until it loads.
  const prev = useRef(saved);
  const [removed, setRemoved] = useState<ReadonlySet<string>>(new Set());
  useEffect(() => {
    const gone = [...prev.current].filter((id) => !saved.has(id));
    const back = [...saved].filter((id) => removed.has(id));
    prev.current = saved;
    if (gone.length || back.length) {
      setRemoved((r) => new Set([...r, ...gone].filter((id) => !back.includes(id))));
    }
  }, [saved, removed]);
  const shown = useMemo(() => auctions?.filter((a) => !removed.has(a.auctionId)) ?? null, [auctions, removed]);

  return (
    <div>
      <div className="page-head">
        <h1>Saved</h1>
      </div>
      <p className="hint" style={{ marginTop: -8 }}>
        Tap the heart on any auction to save it. We&apos;ll email you an hour before it ends (you can turn that off on
        your <Link href="/account">account page</Link>).
      </p>
      {error && <p className="notice bad">{error}</p>}
      {shown == null && !error && <p className="muted">Loading…</p>}
      {shown?.length === 0 && (
        <div className="empty-state">Nothing saved yet. <Link href="/">Find something you like</Link></div>
      )}
      {shown && shown.length > 0 && <AuctionGrid auctions={shown} />}
    </div>
  );
}
