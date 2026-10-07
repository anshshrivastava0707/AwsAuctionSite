"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { createAccount } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function OnboardingForm({ next }: { next: string }) {
  const { status, token, setUser, refresh } = useAuth();
  const router = useRouter();
  const [displayName, setDisplayName] = useState("");
  const [requestSeller, setRequestSeller] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (status === "anonymous") router.replace("/login?signup=1");
    if (status === "ready") router.replace(next);
  }, [status, next, router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      setUser(await createAccount(token, { displayName, requestSeller }));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create your account");
      setBusy(false);
    }
  }

  if (status !== "onboarding") return <p className="muted">Loading…</p>;

  return (
    <div className="card narrow">
      <h1>Set up your account</h1>
      <p className="muted">
        Every account is reviewed by an admin before it can bid. Ask for seller access too if you want to list items.
      </p>
      <form className="stack" onSubmit={onSubmit}>
        <label>
          Display name (shown on your bids and listings)
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required maxLength={40} autoFocus />
        </label>
        <label className="check">
          <input type="checkbox" checked={requestSeller} onChange={(e) => setRequestSeller(e.target.checked)} />
          I also want to sell items
        </label>
        {error && <p className="notice bad">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? "Creating…" : "Create account"}</button>
      </form>
    </div>
  );
}
