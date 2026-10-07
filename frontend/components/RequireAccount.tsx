"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth";
import type { PrivateUser } from "@/lib/types";

/**
 * Renders children only for a logged-in user with an account (and, optionally,
 * an approved role / admin). Everyone else gets a clear next step instead.
 */
export default function RequireAccount({
  children,
  role,
}: {
  children: (user: PrivateUser, token: string) => React.ReactNode;
  role?: "seller" | "admin";
}) {
  const { status, user, token } = useAuth();
  const router = useRouter();
  const path = usePathname();

  useEffect(() => {
    if (status === "anonymous") router.replace(`/login?next=${encodeURIComponent(path)}`);
    if (status === "onboarding") router.replace(`/onboarding?next=${encodeURIComponent(path)}`);
  }, [status, router, path]);

  if (status !== "ready" || !user || !token) return <p className="muted">Loading…</p>;

  if (role === "admin" && !user.isAdmin) {
    return <p className="notice bad">This page is for admins only.</p>;
  }
  if (role === "seller" && user.sellerStatus !== "APPROVED") {
    return (
      <div className="card narrow">
        <h2>Seller access needed</h2>
        <p className="muted">
          {user.sellerStatus === "PENDING"
            ? "Your seller request is waiting for an admin to approve it."
            : "You need an approved seller account to list items."}
        </p>
        <Link href="/account">Go to your account</Link>
      </div>
    );
  }
  return <>{children(user, token)}</>;
}
