import Link from "next/link";
import { imageUrl } from "@/lib/api";
import { formatCents, PHASE_LABEL, PHASE_PILL } from "@/lib/format";
import { CATEGORIES, type Auction } from "@/lib/types";

/** Uses the server-computed phase, so it is safe to render on the server. */
export default function AuctionCard({ auction: a, href }: { auction: Auction; href?: string }) {
  return (
    <Link href={href ?? `/auctions/${a.auctionId}`} className="auction-card">
      {a.images[0] ? (
        // eslint-disable-next-line @next/next/no-img-element -- images are served by our API
        <img className="thumb" src={imageUrl(a.images[0])} alt="" loading="lazy" />
      ) : (
        <div className="thumb empty">No photo</div>
      )}
      <div className="body">
        <span className="title" title={a.title}>{a.title}</span>
        <span className="muted small">
          {CATEGORIES[a.category] ?? a.category}
          {a.quantity > 1 ? ` · lot of ${a.quantity}` : ""} · {a.bidCount} bids
        </span>
        <span className="foot">
          <span className="num">{formatCents(a.currentHigh ?? a.startingPrice)}</span>
          <span className={`pill ${PHASE_PILL[a.phase]}`}>{PHASE_LABEL[a.phase]}</span>
        </span>
      </div>
    </Link>
  );
}
