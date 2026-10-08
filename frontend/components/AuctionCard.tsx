import Link from "next/link";
import { formatPrice } from "@/lib/format";
import { CATEGORIES, type Auction } from "@/lib/types";
import CardStatus from "./CardStatus";
import FavoriteButton from "./FavoriteButton";
import Thumb from "./Thumb";

/** The title link stretches over the whole card; the heart and bid button sit above it. */
export default function AuctionCard({ auction: a, href }: { auction: Auction; href?: string }) {
  const to = href ?? `/auctions/${a.auctionId}`;
  const open = a.phase === "LIVE" || a.phase === "SCHEDULED";
  let priceLabel = a.currentHigh != null ? "Current bid" : "Starting bid";
  if (!open) {
    if (a.soldVia === "BUY_NOW") priceLabel = "Bought now";
    else if (a.currentHigh == null) priceLabel = "No bids";
    else priceLabel = a.reserveMet ? "Winning bid" : "Reserve not met";
  }

  return (
    <article className="auction-card">
      <div className="media">
        {a.images[0] ? <Thumb imageKey={a.images[0]} /> : <div className="thumb empty">No photo yet</div>}
        <CardStatus auction={a} />
        {a.buyNowPrice != null && <span className="buy-now-tag">Buy now {formatPrice(a.buyNowPrice)}</span>}
      </div>
      <div className="body">
        <Link href={to} className="title card-link" title={a.title}>{a.title}</Link>
        <span className="muted">
          {CATEGORIES[a.category] ?? a.category}
          {a.quantity > 1 ? ` · lot of ${a.quantity}` : ""} · {a.bidCount} {a.bidCount === 1 ? "bid" : "bids"}
          {a.watchCount > 0 && ` · ${a.watchCount} watching`}
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
