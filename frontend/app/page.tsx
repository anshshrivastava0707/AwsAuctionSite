import Link from "next/link";
import AuctionCard from "@/components/AuctionCard";
import { listAuctions } from "@/lib/api";
import { CATEGORIES, type Auction, type Category } from "@/lib/types";

// Always render from the database at request time.
export const dynamic = "force-dynamic";

export default async function Home({ searchParams }: { searchParams: Promise<{ category?: string }> }) {
  const { category: raw } = await searchParams;
  const category = raw && raw in CATEGORIES ? (raw as Category) : null;
  let auctions: Auction[] = [];
  let error: string | null = null;
  try {
    auctions = await listAuctions(category);
  } catch (e) {
    error = e instanceof Error ? e.message : "Failed to load auctions";
  }

  return (
    <div>
      <div className="page-head">
        <h1>{category ? CATEGORIES[category] : "All auctions"}</h1>
      </div>
      <div className="chips">
        <Link href="/" className={`chip ${category ? "" : "active"}`}>All</Link>
        {Object.entries(CATEGORIES).map(([k, label]) => (
          <Link key={k} href={`/?category=${k}`} className={`chip ${category === k ? "active" : ""}`}>{label}</Link>
        ))}
      </div>
      {error && <p className="notice bad">{error}</p>}
      {!error && auctions.length === 0 && <p className="muted">No auctions here yet.</p>}
      <div className="cards">
        {auctions.map((a) => <AuctionCard key={a.auctionId} auction={a} />)}
      </div>
    </div>
  );
}
