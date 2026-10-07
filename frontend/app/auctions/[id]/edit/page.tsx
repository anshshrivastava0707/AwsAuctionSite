"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import AuctionForm from "@/components/AuctionForm";
import RequireAccount from "@/components/RequireAccount";
import { getAuction, updateAuction } from "@/lib/api";
import type { Auction, PrivateUser } from "@/lib/types";

export default function EditAuctionPage() {
  const { id } = useParams<{ id: string }>();
  return <RequireAccount role="seller">{(user, token) => <Edit id={id} user={user} token={token} />}</RequireAccount>;
}

function Edit({ id, user, token }: { id: string; user: PrivateUser; token: string }) {
  const router = useRouter();
  const [auction, setAuction] = useState<Auction | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getAuction(id).then((s) => setAuction(s.auction), (e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [id]);

  if (error) return <p className="notice bad">{error}</p>;
  if (!auction) return <p className="muted">Loading…</p>;
  if (auction.sellerId !== user.userId) return <p className="notice bad">Only the seller can edit this auction.</p>;
  if (auction.bidCount > 0 || auction.phase === "ENDED" || auction.phase === "CANCELLED") {
    return (
      <div className="card narrow">
        <p className="notice warn">
          {auction.bidCount > 0 ? "This auction already has bids, so it can no longer be edited."
            : "This auction is over and can't be edited."}
        </p>
        <Link href={`/auctions/${id}`}>Back to the auction</Link>
      </div>
    );
  }

  return (
    <div className="card narrow" style={{ maxWidth: 680 }}>
      <div className="page-head">
        <h1>Edit listing</h1>
        <Link href={`/auctions/${id}`}>Back</Link>
      </div>
      <p className="hint">If someone bids before you save, the edit is refused and the listing stays as it was.</p>
      <AuctionForm
        token={token}
        initial={auction}
        submitLabel="Save changes"
        onSubmit={async (changes) => {
          await updateAuction(token, id, changes);
          router.push(`/auctions/${id}`);
        }}
      />
    </div>
  );
}
