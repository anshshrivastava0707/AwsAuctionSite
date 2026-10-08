"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import Avatar from "./Avatar";
import Dropdown from "./Dropdown";
import { BloomLogo, ChevronDown, SearchIcon } from "./icons";

export default function Header() {
  const { status, user, logout } = useAuth();
  const path = usePathname();
  const link = (href: string, label: string) => (
    <Link href={href} className={path === href ? "active" : undefined}>{label}</Link>
  );

  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Link href="/" className="brand"><BloomLogo /> BidBloom</Link>
        <nav className="nav">
          {link("/", "Explore")}
          {status === "ready" && user && (
            <>
              {user.sellerStatus === "APPROVED" && link("/sell", "Sell")}
              {link("/me/bids", "My bids")}
              {user.sellerStatus === "APPROVED" && link("/me/selling", "Selling")}
              {user.isAdmin && link("/admin", "Admin")}
            </>
          )}
          {status === "onboarding" && link("/onboarding", "Finish sign-up")}
        </nav>
        <div className="topbar-end">
          <Link href="/#search" className="icon-button" aria-label="Search"><SearchIcon size={22} /></Link>
          {status === "ready" && user && (
            <UserMenu name={user.displayName} avatarKey={user.avatarKey} userId={user.userId} onLogout={logout} />
          )}
          {status === "onboarding" && <button className="secondary small" onClick={logout}>Log out</button>}
          {status === "anonymous" && (
            <>
              <Link href="/login" className="nav-plain">Log in</Link>
              <Link href="/login?signup=1" className="button small">Sign up</Link>
            </>
          )}
        </div>
      </div>
    </header>
  );
}

function UserMenu({ name, avatarKey, userId, onLogout }: {
  name: string; avatarKey: string | null; userId: string; onLogout: () => void;
}) {
  return (
    <Dropdown
      align="right"
      label="Account menu"
      triggerClass="menu-trigger"
      trigger={<><Avatar name={name} avatarKey={avatarKey} size={40} /><ChevronDown size={18} /></>}
    >
      <div className="menu-head">{name}</div>
      <Link role="menuitem" href="/account">Account</Link>
      <Link role="menuitem" href={`/users/${userId}`}>Public profile</Link>
      <button role="menuitem" onClick={onLogout}>Log out</button>
    </Dropdown>
  );
}
