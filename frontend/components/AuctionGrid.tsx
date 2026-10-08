"use client";

import { useAuth } from "@/lib/auth";
import type { Auction } from "@/lib/types";
import { useNow } from "@/lib/useNow";
import { useWatchAuctions } from "@/lib/useWatchAuctions";
import AuctionCard from "./AuctionCard";

/** Matches the server's browse rule (models.RECENTLY_ENDED_MS). */
export const RECENTLY_ENDED_MS = 15 * 60 * 1000;

/** Cards whose prices and bid counts follow the live socket. Order stays as given.
 *  `browse`: drop cards 15 minutes after they end (or once cancelled), as the
 *  server does, so a page left open doesn't keep stale auctions around. */
export default function AuctionGrid({ auctions: initial, browse = false }: { auctions: Auction[]; browse?: boolean }) {
  const { status, token, user } = useAuth();
  const { auctions } = useWatchAuctions(initial, user?.userId ?? null, token, status !== "loading");
  const now = useNow(30_000);
  const shown = initial
    .map((a) => auctions[a.auctionId] ?? a)
    .filter((a) => !browse || now == null || (a.status !== "CANCELLED" && now - a.endsAt < RECENTLY_ENDED_MS));
  return (
    <div className="cards">
      {shown.map((a) => <AuctionCard key={a.auctionId} auction={a} />)}
    </div>
  );
}
