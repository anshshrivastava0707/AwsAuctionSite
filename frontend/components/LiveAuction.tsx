"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { adminRemoveAuction, cancelAuction } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  formatCents, formatDateTime, formatRemaining, livePhase, parseDollarsToCents, PHASE_LABEL, PHASE_PILL,
} from "@/lib/format";
import { CATEGORIES, CONDITIONS, type Snapshot } from "@/lib/types";
import { useAuctionSocket, type ConnStatus } from "@/lib/useAuctionSocket";
import { useNow } from "@/lib/useNow";
import FavoriteButton from "./FavoriteButton";
import Gallery from "./Gallery";

const CONN_LABEL: Record<ConnStatus, string> = {
  connecting: "Connecting…",
  live: "Connected",
  reconnecting: "Reconnecting…",
  unconfigured: "Not configured",
};

export default function LiveAuction({ initial }: { initial: Snapshot }) {
  const { status: authStatus, token, user } = useAuth();
  const userId = user?.userId ?? null;
  const { auction, bids, conn, lastResult, pendingCount, error, placeBid } = useAuctionSocket(
    initial, token, userId, authStatus !== "loading",
  );
  const now = useNow();
  const router = useRouter();
  const [amount, setAmount] = useState("");
  const [auto, setAuto] = useState(false);
  const [maxAmount, setMaxAmount] = useState("");
  const [yourMax, setYourMax] = useState<number | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  // Pre-fill the minimum valid next bid whenever it moves (and the field is empty or now too low).
  useEffect(() => {
    setAmount((prev) => {
      const cents = parseDollarsToCents(prev);
      return cents == null || cents < auction.minNextBid ? (auction.minNextBid / 100).toFixed(2) : prev;
    });
  }, [auction.minNextBid]);

  // Remember the maximum the server confirmed for me (it's private, so only my own replies carry it).
  useEffect(() => {
    if (lastResult?.status === "ACCEPTED" && lastResult.yourMax != null) setYourMax(lastResult.yourMax);
  }, [lastResult]);

  const phase = livePhase(auction, now);
  const isSeller = userId != null && auction.sellerId === userId;
  const youLead = userId != null && auction.highBidderId === userId;
  const youWon = youLead && phase === "ENDED" && auction.reserveMet;
  const canManage = isSeller && auction.bidCount === 0 && (phase === "LIVE" || phase === "SCHEDULED");

  // Why this user can't bid right now (null = they can).
  let blocked: React.ReactNode = null;
  if (phase === "SCHEDULED") blocked = `Bidding opens ${formatDateTime(auction.startsAt)}.`;
  else if (phase !== "LIVE") blocked = phase === "CANCELLED" ? "This auction was cancelled." : "This auction has ended.";
  else if (authStatus === "loading") blocked = "Loading…";
  else if (authStatus === "anonymous")
    blocked = <><Link href={`/login?next=/auctions/${auction.auctionId}`}>Log in</Link> to place a bid.</>;
  else if (authStatus === "onboarding") blocked = <><Link href="/onboarding">Finish creating your account</Link> to bid.</>;
  else if (isSeller) blocked = "This is your auction, so you can't bid on it.";
  else if (user?.buyerStatus !== "APPROVED")
    blocked = user?.buyerStatus === "REJECTED"
      ? "Your account was not approved for bidding."
      : "Your account is waiting for an admin to approve it for bidding.";

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (auto) {
      // Automatic bidding: bid the minimum now, and let the server go up to the maximum.
      const max = parseDollarsToCents(maxAmount);
      if (max == null) return setFormError("Enter your maximum as a dollar amount.");
      const floor = youLead ? (yourMax ?? auction.currentHigh ?? 0) + 1 : auction.minNextBid;
      if (max < floor) {
        return setFormError(youLead
          ? `You're already winning. Enter a maximum above ${formatCents(floor - 1)}.`
          : `Your maximum must be at least ${formatCents(auction.minNextBid)}.`);
      }
      setFormError(null);
      placeBid(youLead ? Math.min(auction.minNextBid, max) : auction.minNextBid, { maxAmount: max });
      return;
    }
    const cents = parseDollarsToCents(amount);
    if (cents == null) return setFormError("Enter a valid dollar amount.");
    if (cents < auction.minNextBid) return setFormError(`Minimum bid is ${formatCents(auction.minNextBid)}.`);
    setFormError(null);
    placeBid(cents);
  }

  function onBuyNow() {
    if (auction.buyNowPrice == null) return;
    if (!window.confirm(`Buy “${auction.title}” now for ${formatCents(auction.buyNowPrice)}? This ends the auction.`)) return;
    placeBid(auction.buyNowPrice, { buyNow: true });
  }

  const endingSoon = phase === "LIVE" && now != null && auction.endsAt - now < 2 * 60 * 1000;

  async function onRemove() {
    if (!token) return;
    const reason = window.prompt(
      "Remove this auction for breaking the terms?\n\nIt disappears from the site, any bids stop counting, and the seller " +
      "and leading bidder are emailed. This can't be undone.\n\nReason (shown to the seller):",
    );
    if (reason == null) return;
    if (!reason.trim()) return window.alert("Please give a reason; the seller will see it.");
    setCancelling(true);
    try {
      await adminRemoveAuction(token, auction.auctionId, reason.trim());
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Could not remove");
    } finally {
      setCancelling(false);
    }
  }

  async function onCancel() {
    if (!token || !window.confirm("Cancel this auction? This can't be undone.")) return;
    setCancelling(true);
    try {
      await cancelAuction(token, auction.auctionId);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Could not cancel");
    } finally {
      setCancelling(false);
    }
  }

  return (
    <div className="grid">
      <section className="card stack-lg">
        <Gallery images={auction.images} title={auction.title} />
        <div>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
            <h1>{auction.title}</h1>
            <span className="actions">
              {!isSeller && <FavoriteButton auctionId={auction.auctionId} title={auction.title} />}
              <span className={`pill ${PHASE_PILL[phase]}`}>{PHASE_LABEL[phase]}</span>
              {/* Connection status, not the auction's: only worth showing while bidding is possible. */}
              {(phase === "LIVE" || phase === "SCHEDULED") && (
                <span className={`pill ${conn}`} title="Real-time updates">
                  <span className="dot" /> {CONN_LABEL[conn]}
                </span>
              )}
            </span>
          </div>
          <div className="meta">
            <span>{CATEGORIES[auction.category]}</span>
            <span>Condition: {CONDITIONS[auction.condition]}</span>
            {auction.quantity > 1 && <span>Lot of {auction.quantity} (sold together)</span>}
            {auction.sellerId && (
              <span>Seller: <Link href={`/users/${auction.sellerId}`}>{isSeller ? "you" : auction.sellerName}</Link></span>
            )}
            {auction.watchCount > 0 && <span>{auction.watchCount} watching</span>}
          </div>
          {auction.description && <p style={{ whiteSpace: "pre-wrap" }}>{auction.description}</p>}
        </div>

        <div>
          <div className="label muted small">{auction.currentHigh == null ? "Starting price" : "Current high bid"}</div>
          <div className="price num">{formatCents(auction.currentHigh ?? auction.startingPrice)}</div>
          <div className="muted">
            {auction.highBidderName ? (
              <>by <span className={youLead ? "you" : undefined}>{youLead ? "you" : auction.highBidderName}</span></>
            ) : (
              "No bids yet"
            )}
            {auction.hasReserve && (
              <span className={`status ${auction.reserveMet ? "APPROVED" : "PENDING"}`} style={{ marginLeft: 10 }}>
                {auction.reserveMet ? "Reserve met" : "Reserve not met"}
              </span>
            )}
          </div>

          <div className="stats">
            <div>
              <span className="label">{phase === "SCHEDULED" ? "Starts in" : "Time left"}</span>
              <span className="num">
                {phase === "CANCELLED" ? "—" : phase === "ENDED" ? "Ended" : now == null ? "—"
                  : formatRemaining((phase === "SCHEDULED" ? auction.startsAt : auction.endsAt) - now)}
              </span>
            </div>
            <div>
              <span className="label">Bids</span>
              <span className="num">{auction.bidCount}</span>
            </div>
            <div>
              <span className="label">Next minimum</span>
              <span className="num">{formatCents(auction.minNextBid)}</span>
            </div>
            <div>
              <span className="label">Ends</span>
              <span className="num">{formatDateTime(auction.endsAt)}</span>
            </div>
          </div>
        </div>

        {phase === "ENDED" && auction.highBidderName && (auction.reserveMet ? (
          <p className={`notice ${youWon ? "ok" : "warn"}`}>
            {youWon
              ? auction.soldVia === "BUY_NOW" ? "You bought this item!" : "You won this auction!"
              : auction.soldVia === "BUY_NOW"
                ? `Bought by ${auction.highBidderName} for ${formatCents(auction.currentHigh)}.`
                : `Won by ${auction.highBidderName} at ${formatCents(auction.currentHigh)}.`}
          </p>
        ) : (
          <p className="notice warn">This auction ended below the seller&apos;s reserve price, so the item didn&apos;t sell.</p>
        ))}
        {phase === "CANCELLED" && (
          <p className="notice bad">
            {auction.removed
              ? "This auction was removed by BidBloom because it doesn't meet our terms. Any bids on it no longer stand."
              : "The seller cancelled this auction."}
          </p>
        )}

        {user?.isAdmin && phase !== "CANCELLED" && (
          <div className="actions admin-bar">
            <span className="hint">Admin</span>
            <button className="danger small" onClick={onRemove} disabled={cancelling}>
              {cancelling ? "Removing…" : "Remove auction"}
            </button>
            <span className="hint">For listings that break the terms. Works even after bids or after it ends.</span>
          </div>
        )}

        {canManage && (
          <div className="actions">
            <button className="secondary" onClick={() => router.push(`/auctions/${auction.auctionId}/edit`)}>
              Edit listing
            </button>
            <button className="danger" onClick={onCancel} disabled={cancelling}>
              {cancelling ? "Cancelling…" : "Cancel auction"}
            </button>
            <span className="hint">You can edit or cancel until the first bid arrives.</span>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Place a bid</h2>
        {blocked ? (
          <p className="notice warn">{blocked}</p>
        ) : (
          <form className="stack" onSubmit={onSubmit}>
            <div className="small muted">Bidding as <strong>{user?.displayName}</strong></div>
            {youLead && (
              <p className="notice ok">
                You&apos;re the highest bidder{yourMax != null && yourMax > (auction.currentHigh ?? 0)
                  ? `, with automatic bids up to ${formatCents(yourMax)}` : ""}.
              </p>
            )}
            <div className="segmented" role="radiogroup" aria-label="Bid type">
              <button type="button" role="radio" aria-checked={!auto} className={!auto ? "on" : ""} onClick={() => setAuto(false)}>
                Bid now
              </button>
              <button type="button" role="radio" aria-checked={auto} className={auto ? "on" : ""} onClick={() => setAuto(true)}>
                Automatic bidding
              </button>
            </div>
            {auto ? (
              <label>
                Your maximum ($)
                <input value={maxAmount} onChange={(e) => setMaxAmount(e.target.value)} inputMode="decimal"
                  placeholder={(auction.minNextBid / 100).toFixed(2)} />
                <span className="hint">
                  We bid for you, one increment at a time, only as much as needed to keep you in the lead — up to
                  this amount. Nobody else sees your maximum.
                </span>
              </label>
            ) : (
              <label>
                Amount ($)
                <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
              </label>
            )}
            <button type="submit">{auto ? (youLead ? "Raise my maximum" : "Start automatic bidding") : "Bid"}</button>
            {auction.buyNowPrice != null && (
              <button type="button" className="buy-now" onClick={onBuyNow}>
                Buy it now for {formatCents(auction.buyNowPrice)}
              </button>
            )}
            {formError && <p className="notice bad">{formError}</p>}
          </form>
        )}
        {phase === "LIVE" && (
          <p className={`hint${endingSoon ? " soft-close" : ""}`} style={{ marginTop: 10 }}>
            {endingSoon ? "Ending soon — " : ""}A bid in the final 2 minutes extends the auction by 2 minutes, so
            there&apos;s always time to respond.
          </p>
        )}
        <div className="stack" style={{ marginTop: 10 }}>
          {pendingCount > 0 && (
            <p className="notice warn">
              {conn === "live" ? "Sending bid…" : "Bid queued — it will be sent when the connection is back."}
            </p>
          )}
          {lastResult && pendingCount === 0 && (
            <p className={`notice ${lastResult.status !== "ACCEPTED" ? "bad" : lastResult.leading === false ? "warn" : "ok"}`}>
              {lastResult.message}
              {lastResult.extendedTo ? " Your late bid extended the auction by 2 minutes." : ""}
            </p>
          )}
          {error && <p className="notice bad">{error}</p>}
        </div>

        <h2 style={{ marginTop: 20 }}>Bid history</h2>
        {bids.length === 0 ? (
          <p className="muted small">No bids yet.</p>
        ) : (
          <ul className="list">
            {bids.map((b) => (
              <li key={b.bidId ?? `${b.amount}:${b.bidderId}`}>
                <span className={b.bidderId === userId ? "you" : undefined}>
                  {b.bidderId === userId ? "You" : b.bidderName}
                  {b.auto && <span className="auto-tag" title="Placed automatically, up to the bidder's maximum">auto</span>}
                </span>
                <span className="num">{formatCents(b.amount)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
