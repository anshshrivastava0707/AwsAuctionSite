"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import Avatar from "./Avatar";

export default function Header() {
  const { status, user, logout } = useAuth();
  const path = usePathname();
  const link = (href: string, label: string) => (
    <Link href={href} className={path === href ? "active" : undefined}>{label}</Link>
  );

  return (
    <header className="topbar">
      <Link href="/" className="brand">Live Auctions</Link>
      <nav className="nav">
        {link("/", "Browse")}
        {status === "ready" && user && (
          <>
            {user.sellerStatus === "APPROVED" && link("/sell", "Sell")}
            {link("/me/bids", "My bids")}
            {user.sellerStatus === "APPROVED" && link("/me/selling", "Selling")}
            {user.isAdmin && link("/admin", "Admin")}
            <Link href="/account" className="who">
              <Avatar name={user.displayName} avatarKey={user.avatarKey} size={24} /> {user.displayName}
            </Link>
            <button className="secondary small" onClick={logout}>Log out</button>
          </>
        )}
        {status === "onboarding" && (
          <>
            {link("/onboarding", "Finish sign-up")}
            <button className="secondary small" onClick={logout}>Log out</button>
          </>
        )}
        {status === "anonymous" && (
          <>
            {link("/login", "Log in")}
            <Link href="/login?signup=1" className="button small">Sign up</Link>
          </>
        )}
      </nav>
    </header>
  );
}
