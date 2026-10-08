"use client";

import Link from "next/link";
import { useState } from "react";
import Avatar from "@/components/Avatar";
import ImagePicker from "@/components/ImagePicker";
import RequireAccount from "@/components/RequireAccount";
import { requestSellerAccess, updateProfile } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { ApprovalStatus, PrivateUser } from "@/lib/types";

const STATUS_TEXT: Record<ApprovalStatus, string> = {
  NONE: "Not requested",
  PENDING: "Waiting for approval",
  APPROVED: "Approved",
  REJECTED: "Not approved",
};

export default function AccountPage() {
  return <RequireAccount>{(user, token) => <Account user={user} token={token} />}</RequireAccount>;
}

function Account({ user, token }: { user: PrivateUser; token: string }) {
  const { setUser } = useAuth();
  const [displayName, setDisplayName] = useState(user.displayName);
  const [bio, setBio] = useState(user.bio);
  const [location, setLocation] = useState(user.location);
  const [avatar, setAvatar] = useState<string[]>(user.avatarKey ? [user.avatarKey] : []);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    const changes: Parameters<typeof updateProfile>[1] = {};
    if (displayName.trim() !== user.displayName) changes.displayName = displayName;
    if (bio.trim() !== user.bio) changes.bio = bio;
    if (location.trim() !== user.location) changes.location = location;
    const avatarKey = avatar[0] ?? null;
    if (avatarKey !== user.avatarKey) changes.avatarKey = avatarKey;
    if (Object.keys(changes).length === 0) return setMessage({ ok: true, text: "Nothing to save." });
    setBusy(true);
    try {
      setUser(await updateProfile(token, changes));
      setMessage({ ok: true, text: "Profile saved." });
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : "Could not save" });
    } finally {
      setBusy(false);
    }
  }

  async function setEmails(on: boolean) {
    try {
      setUser(await updateProfile(token, { emailNotifications: on }));
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : "Could not save" });
    }
  }

  async function askSeller() {
    try {
      setUser(await requestSellerAccess(token));
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : "Request failed" });
    }
  }

  return (
    <div className="grid">
      <section className="card">
        <div className="profile-head" style={{ marginBottom: 16 }}>
          <Avatar name={user.displayName} avatarKey={user.avatarKey} size={56} />
          <div>
            <h1 style={{ margin: 0 }}>{user.displayName}</h1>
            <div className="muted small">{user.email} · <Link href={`/users/${user.userId}`}>public profile</Link></div>
          </div>
        </div>
        <form className="stack" onSubmit={save}>
          <label>
            Display name
            <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required maxLength={40} />
          </label>
          <label>
            Location
            <input value={location} onChange={(e) => setLocation(e.target.value)} maxLength={80} />
          </label>
          <label>
            About you
            <textarea value={bio} onChange={(e) => setBio(e.target.value)} rows={3} maxLength={500} />
          </label>
          <div>
            <div className="small muted" style={{ marginBottom: 4 }}>Profile picture</div>
            <div style={{ maxWidth: 120 }}>
              <ImagePicker token={token} value={avatar} onChange={setAvatar} max={1} />
            </div>
          </div>
          {message && <p className={`notice ${message.ok ? "ok" : "bad"}`}>{message.text}</p>}
          <button type="submit" disabled={busy}>{busy ? "Saving…" : "Save profile"}</button>
        </form>
      </section>

      <section className="card stack">
        <h2>Account status</h2>
        <div className="actions">
          Buying: <span className={`status ${user.buyerStatus}`}>{STATUS_TEXT[user.buyerStatus]}</span>
        </div>
        <div className="actions">
          Selling: <span className={`status ${user.sellerStatus}`}>{STATUS_TEXT[user.sellerStatus]}</span>
        </div>
        {(user.sellerStatus === "NONE" || user.sellerStatus === "REJECTED") && (
          <button className="secondary" onClick={askSeller}>
            {user.sellerStatus === "REJECTED" ? "Ask again for seller access" : "Request seller access"}
          </button>
        )}
        {user.isAdmin && <p className="small muted">You are an admin. <Link href="/admin">Review accounts</Link></p>}
        <p className="small muted">An admin reviews each role. You will be able to bid or list once approved.</p>
        <h2 style={{ marginTop: 12 }}>Email notifications</h2>
        <label className="check">
          <input type="checkbox" checked={user.emailNotifications} onChange={(e) => void setEmails(e.target.checked)} />
          Email me when I&apos;m outbid, when I win, when my items sell, and an hour before saved auctions end
        </label>
        <div className="actions">
          <Link href="/me/bids">My bidding history</Link>
          <Link href="/me/saved">Saved auctions</Link>
          {user.sellerStatus === "APPROVED" && <Link href="/me/selling">Items I&apos;m selling</Link>}
        </div>
      </section>
    </div>
  );
}
