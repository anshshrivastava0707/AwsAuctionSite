"use client";

import { formatClock, livePhase } from "@/lib/format";
import type { Auction } from "@/lib/types";
import { useNow } from "@/lib/useNow";

/** The badge in a card's corner: phase plus a ticking countdown. */
export default function CardStatus({ auction }: { auction: Auction }) {
  const now = useNow();
  const phase = livePhase(auction, now);
  if (phase === "LIVE") {
    return (
      <div className="badge-stack">
        <span className="badge live"><span className="dot" /> LIVE</span>
        <span className="badge clock num">{now == null ? "--:--:--" : formatClock(auction.endsAt - now)}</span>
      </div>
    );
  }
  if (phase === "SCHEDULED") {
    return (
      <div className="badge-stack">
        <span className="badge soon">UPCOMING</span>
        {now != null && <span className="badge clock num">in {formatClock(auction.startsAt - now)}</span>}
      </div>
    );
  }
  return (
    <div className="badge-stack">
      <span className="badge over">{phase === "ENDED" ? "ENDED" : "CANCELLED"}</span>
    </div>
  );
}
