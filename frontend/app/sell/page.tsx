"use client";

import { useRouter } from "next/navigation";
import AuctionForm from "@/components/AuctionForm";
import RequireAccount from "@/components/RequireAccount";
import { createAuction } from "@/lib/api";
import type { ListingInput } from "@/lib/types";

export default function SellPage() {
  const router = useRouter();
  return (
    <RequireAccount role="seller">
      {(_user, token) => (
        <div className="card narrow" style={{ maxWidth: 680 }}>
          <h1>List an item</h1>
          <AuctionForm
            token={token}
            submitLabel="Create auction"
            onSubmit={async (input) => {
              const auction = await createAuction(token, input as ListingInput);
              router.push(`/auctions/${auction.auctionId}`);
            }}
          />
        </div>
      )}
    </RequireAccount>
  );
}
