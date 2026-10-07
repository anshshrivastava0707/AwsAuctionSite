"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth";

export default function LoginForm({ next, signup }: { next: string; signup: boolean }) {
  const { status, login } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (status === "ready") router.replace(next);
    // New accounts land on /account (to see their approval status) unless they were headed somewhere.
    if (status === "onboarding") router.replace(next === "/" ? "/onboarding" : `/onboarding?next=${encodeURIComponent(next)}`);
  }, [status, next, router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
      setBusy(false);
    }
  }

  return (
    <div className="card narrow">
      <h1>{signup ? "Create an account" : "Log in"}</h1>
      <p className="notice warn">
        Development login: there are no passwords yet, so anyone can sign in as any email. Password reset and
        email verification arrive with the real auth provider.
      </p>
      <form className="stack" onSubmit={onSubmit}>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
        </label>
        {error && <p className="notice bad">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? "Signing in…" : signup ? "Continue" : "Log in"}</button>
      </form>
      <p className="small muted">New here? Signing in with a new email starts account setup.</p>
    </div>
  );
}
