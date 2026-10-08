"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { cancelAuction } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  formatCents, formatDateTime, formatRemaining, livePhase, parseDollarsToCents, PHASE_LABEL, PHASE_PILL,
} from "@/lib/format";
import { CATEGORIES, CONDITIONS, type Snapshot } from "@/lib/types";
import { useAuctionSocket, type ConnStatus } from "@/lib/useAuctionSocket";
import { useNow } from "@/lib/useNow";
import Gallery from "./Gallery";

const CONN_LABEL: Record<ConnStatus, string> = {
  connecting: "Connecting…",
  live: "Live",
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
  const [formError, setFormError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  // Pre-fill the minimum valid next bid whenever it moves (and the field is empty or now too low).
  useEffect(() => {
    setAmount((prev) => {
      const cents = parseDollarsToCents(prev);
      return cents == null || cents < auction.minNextBid ? (auction.minNextBid / 100).toFixed(2) : prev;
    });
  }, [auction.minNextBid]);

  const phase = livePhase(auction, now);
  const isSeller = userId != null && auction.sellerId === userId;
  const youLead = userId != null && auction.highBidderId === userId;
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
    const cents = parseDollarsToCents(amount);
    if (cents == null) return setFormError("Enter a valid dollar amount.");
    if (cents < auction.minNextBid) return setFormError(`Minimum bid is ${formatCents(auction.minNextBid)}.`);
    setFormError(null);
    placeBid(cents);
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
              <span className={`pill ${PHASE_PILL[phase]}`}>{PHASE_LABEL[phase]}</span>
              <span className={`pill ${conn}`} title="Real-time connection">
                <span className="dot" /> {CONN_LABEL[conn]}
              </span>
            </span>
          </div>
          <div className="meta">
            <span>{CATEGORIES[auction.category]}</span>
            <span>Condition: {CONDITIONS[auction.condition]}</span>
            {auction.quantity > 1 && <span>Lot of {auction.quantity} (sold together)</span>}
            {auction.sellerId && (
              <span>Seller: <Link href={`/users/${auction.sellerId}`}>{isSeller ? "you" : auction.sellerName}</Link></span>
            )}
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

        {phase === "ENDED" && auction.highBidderName && (
          <p className={`notice ${youLead ? "ok" : "warn"}`}>
            {youLead ? "You won this auction!" : `Won by ${auction.highBidderName} at ${formatCents(auction.currentHigh)}.`}
          </p>
        )}
        {phase === "CANCELLED" && <p className="notice bad">The seller cancelled this auction.</p>}

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
            <label>
              Amount ($)
              <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
            </label>
            <button type="submit">Bid</button>
            {formError && <p className="notice bad">{formError}</p>}
          </form>
        )}
        <div className="stack" style={{ marginTop: 10 }}>
          {pendingCount > 0 && (
            <p className="notice warn">
              {conn === "live" ? "Sending bid…" : "Bid queued — it will be sent when the connection is back."}
            </p>
          )}
          {lastResult && pendingCount === 0 && (
            <p className={`notice ${lastResult.status === "ACCEPTED" ? "ok" : "bad"}`}>{lastResult.message}</p>
          )}
          {error && <p className="notice bad">{error}</p>}
        </div>

        <h2 style={{ marginTop: 20 }}>Bid history</h2>
        {bids.length === 0 ? (
          <p className="muted small">No bids yet.</p>
        ) : (
          <ul className="list">
            {bids.map((b) => (
              <li key={b.amount}>
                <span className={b.bidderId === userId ? "you" : undefined}>
                  {b.bidderId === userId ? "You" : b.bidderName}
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
