import { notFound } from "next/navigation";
import LiveAuction from "@/components/LiveAuction";
import { ApiError, getAuction } from "@/lib/api";

// Server-render from a strongly consistent read so a fresh page load shows the
// current persisted state immediately; the WebSocket then keeps it live.
export const dynamic = "force-dynamic";

export default async function AuctionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    const snapshot = await getAuction(id);
    return <LiveAuction initial={snapshot} />;
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    return <p className="notice bad">{e instanceof Error ? e.message : "Failed to load auction"}</p>;
  }
}
