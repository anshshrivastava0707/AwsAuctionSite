import { notFound } from "next/navigation";
import AuctionCard from "@/components/AuctionCard";
import Avatar from "@/components/Avatar";
import { ApiError, getUser, getUserAuctions } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function UserPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    const [user, auctions] = await Promise.all([getUser(id), getUserAuctions(id)]);
    return (
      <div className="stack-lg">
        <section className="card">
          <div className="profile-head">
            <Avatar name={user.displayName} avatarKey={user.avatarKey} size={72} />
            <div>
              <h1 style={{ margin: 0 }}>{user.displayName}</h1>
              <div className="meta">
                {user.location && <span>{user.location}</span>}
                <span>Member since {formatDateTime(user.createdAt)}</span>
                {user.isSeller && <span className="status APPROVED">Approved seller</span>}
              </div>
            </div>
          </div>
          {user.bio && <p style={{ whiteSpace: "pre-wrap" }}>{user.bio}</p>}
        </section>
        {(user.isSeller || auctions.length > 0) && (
          <section>
            <h2>Listings</h2>
            {auctions.length === 0 ? <p className="muted">No listings yet.</p> : (
              <div className="cards">{auctions.map((a) => <AuctionCard key={a.auctionId} auction={a} />)}</div>
            )}
          </section>
        )}
      </div>
    );
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    return <p className="notice bad">{e instanceof Error ? e.message : "Failed to load profile"}</p>;
  }
}
