"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import Avatar from "@/components/Avatar";
import RequireAccount from "@/components/RequireAccount";
import { adminListUsers, adminSetApproval } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { ApprovalStatus, PrivateUser } from "@/lib/types";

export default function AdminPage() {
  return <RequireAccount role="admin">{(_user, token) => <Admin token={token} />}</RequireAccount>;
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
