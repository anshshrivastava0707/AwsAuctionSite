import Link from "next/link";
import { imageUrl } from "@/lib/api";
import { formatPrice } from "@/lib/format";
import { CATEGORIES, type Auction } from "@/lib/types";
import CardStatus from "./CardStatus";
import FavoriteButton from "./FavoriteButton";

/** The title link stretches over the whole card; the heart and bid button sit above it. */
export default function AuctionCard({ auction: a, href }: { auction: Auction; href?: string }) {
  const to = href ?? `/auctions/${a.auctionId}`;
  const open = a.phase === "LIVE" || a.phase === "SCHEDULED";
  const priceLabel = a.currentHigh != null
    ? (open ? "Current bid" : "Winning bid")
    : (open ? "Starting bid" : "No bids");

  return (
    <article className="auction-card">
      <div className="media">
        {a.images[0] ? (
          // eslint-disable-next-line @next/next/no-img-element -- images are served by our API
          <img className="thumb" src={imageUrl(a.images[0])} alt="" loading="lazy" />
        ) : (
          <div className="thumb empty">No photo yet</div>
        )}
        <CardStatus auction={a} />
      </div>
      <div className="body">
        <Link href={to} className="title card-link" title={a.title}>{a.title}</Link>
        <span className="muted">
          {CATEGORIES[a.category] ?? a.category}
          {a.quantity > 1 ? ` · lot of ${a.quantity}` : ""} · {a.bidCount} {a.bidCount === 1 ? "bid" : "bids"}
        </span>
        <div className="foot">
          <div className="price-block">
            <span className="card-price num">{formatPrice(a.currentHigh ?? a.startingPrice)}</span>
            <span className="muted small">{priceLabel}</span>
          </div>
          <div className="card-actions">
            <FavoriteButton auctionId={a.auctionId} title={a.title} />
            <Link href={to} className={`button${a.phase === "LIVE" ? "" : " secondary"}`}>
              {a.phase === "LIVE" ? "Place bid" : "View"}
            </Link>
          </div>
        </div>
      </div>
    </article>
  );
}
