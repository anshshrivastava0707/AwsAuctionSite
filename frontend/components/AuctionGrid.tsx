"use client";

import { useAuth } from "@/lib/auth";
import type { Auction } from "@/lib/types";
import { useWatchAuctions } from "@/lib/useWatchAuctions";
import AuctionCard from "./AuctionCard";

/** Cards whose prices and bid counts follow the live socket. Order stays as given. */
export default function AuctionGrid({ auctions: initial }: { auctions: Auction[] }) {
  const { status, token, user } = useAuth();
  const { auctions } = useWatchAuctions(initial, user?.userId ?? null, token, status !== "loading");
  return (
    <div className="cards">
      {initial.map((a) => <AuctionCard key={a.auctionId} auction={auctions[a.auctionId] ?? a} />)}
    </div>
  );
}
