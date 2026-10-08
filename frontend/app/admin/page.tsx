"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import Avatar from "@/components/Avatar";
import RequireAccount from "@/components/RequireAccount";
import { adminListUsers, adminRemoveAuction, adminSetApproval, listAuctions } from "@/lib/api";
import { formatCents, formatDateTime, PHASE_LABEL, PHASE_PILL } from "@/lib/format";
import type { ApprovalStatus, Auction, PrivateUser } from "@/lib/types";

export default function AdminPage() {
  const [tab, setTab] = useState<"accounts" | "auctions">("accounts");
  return (
    <RequireAccount role="admin">
      {(_user, token) => (
        <div className="stack-lg">
          <div className="chips" role="tablist" style={{ margin: 0 }}>
            <button role="tab" aria-selected={tab === "accounts"} className={`chip ${tab === "accounts" ? "active" : ""}`}
              onClick={() => setTab("accounts")}>Accounts</button>
            <button role="tab" aria-selected={tab === "auctions"} className={`chip ${tab === "auctions" ? "active" : ""}`}
              onClick={() => setTab("auctions")}>Auctions</button>
          </div>
          {tab === "accounts" ? <Admin token={token} /> : <Auctions token={token} />}
        </div>
      )}
    </RequireAccount>
  );
}

/** Find listings and take down any that break the terms. */
function Auctions({ token }: { token: string }) {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [auctions, setAuctions] = useState<Auction[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    setAuctions(null);
    setError(null);
    listAuctions({ q: query || undefined, sort: "newest", limit: 50 }).then(
      (page) => { setAuctions(page.auctions); setCursor(page.nextCursor); },
      (e) => setError(e instanceof Error ? e.message : "Failed to load"),
    );
  }, [query]);

  async function more() {
    const page = await listAuctions({ q: query || undefined, sort: "newest", limit: 50, cursor });
    setAuctions((list) => [...(list ?? []), ...page.auctions]);
    setCursor(page.nextCursor);
  }

  async function remove(a: Auction) {
    const reason = window.prompt(
      `Remove “${a.title}”?\n\nIt disappears from the site, any bids stop counting, and the seller and leading ` +
      "bidder are emailed. This can't be undone.\n\nReason (shown to the seller):",
    );
    if (reason == null) return;
    if (!reason.trim()) return window.alert("Please give a reason; the seller will see it.");
    setBusy(a.auctionId);
    try {
      await adminRemoveAuction(token, a.auctionId, reason.trim());
      setAuctions((list) => list?.filter((x) => x.auctionId !== a.auctionId) ?? null);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "Could not remove");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="card">
      <div className="page-head">
        <h1>Auctions</h1>
        <form className="actions" onSubmit={(e) => { e.preventDefault(); setQuery(q.trim()); }}>
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search title, seller…" />
          <button type="submit" className="secondary small">Search</button>
        </form>
      </div>
      {error && <p className="notice bad">{error}</p>}
      {auctions == null && !error && <p className="muted">Loading…</p>}
      {auctions?.length === 0 && <p className="muted">No auctions found.</p>}
      {auctions && auctions.length > 0 && (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th>Auction</th><th>Seller</th><th>Status</th><th>Price</th><th>Bids</th><th>Ends</th><th /></tr></thead>
            <tbody>
              {auctions.map((a) => (
                <tr key={a.auctionId}>
                  <td><Link href={`/auctions/${a.auctionId}`}>{a.title}</Link></td>
                  <td className="small">{a.sellerId ? <Link href={`/users/${a.sellerId}`}>{a.sellerName}</Link> : "—"}</td>
                  <td><span className={`pill ${PHASE_PILL[a.phase]}`}>{PHASE_LABEL[a.phase]}</span></td>
                  <td className="num">{formatCents(a.currentHigh ?? a.startingPrice)}</td>
                  <td className="num">{a.bidCount}</td>
                  <td className="small">{formatDateTime(a.endsAt)}</td>
                  <td>
                    <button className="danger small" onClick={() => remove(a)} disabled={busy === a.auctionId}>
                      {busy === a.auctionId ? "Removing…" : "Remove"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {cursor && <div className="load-more"><button className="secondary" onClick={more}>Load more</button></div>}
      <p className="hint" style={{ marginTop: 12 }}>
        Removing works on live, upcoming and ended auctions, even after bids. Removed and cancelled listings don&apos;t
        appear here or anywhere else on the site; sellers still see them, with your reason, under Selling.
      </p>
    </div>
  );
}

type Role = "buyerStatus" | "sellerStatus";

function Admin({ token }: { token: string }) {
  const [filter, setFilter] = useState<"pending" | "all">("pending");
  const [users, setUsers] = useState<PrivateUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setUsers(null);
    adminListUsers(token, filter).then(setUsers, (e) => setError(e instanceof Error ? e.message : "Failed to load"));
  }, [token, filter]);
  useEffect(load, [load]);

  async function set(user: PrivateUser, role: Role, value: ApprovalStatus) {
    try {
      const updated = await adminSetApproval(token, user.userId, { [role]: value });
      setUsers((list) => list?.map((u) => (u.userId === updated.userId ? updated : u)) ?? null);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "Update failed");
    }
  }

  const controls = (u: PrivateUser, role: Role) => {
    const value = u[role];
    return (
      <span className="actions">
        <span className={`status ${value}`}>{value}</span>
        {value !== "APPROVED" && (role === "buyerStatus" || value === "PENDING") && (
          <button className="small" onClick={() => set(u, role, "APPROVED")}>Approve</button>
        )}
        {value === "PENDING" && <button className="secondary small" onClick={() => set(u, role, "REJECTED")}>Reject</button>}
        {value === "APPROVED" && <button className="secondary small" onClick={() => set(u, role, "REJECTED")}>Revoke</button>}
      </span>
    );
  };

  return (
    <div className="card">
      <div className="page-head">
        <h1>Account approvals</h1>
        <div className="chips" style={{ margin: 0 }}>
          <button className={`chip ${filter === "pending" ? "active" : ""}`} onClick={() => setFilter("pending")}>Pending</button>
          <button className={`chip ${filter === "all" ? "active" : ""}`} onClick={() => setFilter("all")}>All accounts</button>
        </div>
      </div>
      {error && <p className="notice bad">{error}</p>}
      {users == null && !error && <p className="muted">Loading…</p>}
      {users?.length === 0 && <p className="muted">{filter === "pending" ? "Nothing waiting for review." : "No accounts yet."}</p>}
      {users && users.length > 0 && (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th>User</th><th>Email</th><th>Joined</th><th>Buying</th><th>Selling</th></tr></thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.userId}>
                  <td>
                    <span className="actions">
                      <Avatar name={u.displayName} avatarKey={u.avatarKey} size={24} />
                      <Link href={`/users/${u.userId}`}>{u.displayName}</Link>
                      {u.isAdmin && <span className="hint">admin</span>}
                    </span>
                  </td>
                  <td className="small">{u.email}</td>
                  <td className="small">{formatDateTime(u.createdAt)}</td>
                  <td>{controls(u, "buyerStatus")}</td>
                  <td>{controls(u, "sellerStatus")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="hint" style={{ marginTop: 12 }}>
        Revoking buyer approval blocks that account&apos;s next bid immediately; bids already placed stay valid.
      </p>
    </div>
  );
}
