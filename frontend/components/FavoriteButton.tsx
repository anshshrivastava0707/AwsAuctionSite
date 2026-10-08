"use client";

import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { toggleFavorite, useFavorites } from "@/lib/useFavorites";
import { HeartIcon } from "./icons";

export default function FavoriteButton({ auctionId, title }: { auctionId: string; title: string }) {
  const saved = useFavorites().has(auctionId);
  const { status, token } = useAuth();
  const router = useRouter();

  function onClick() {
    if (status !== "ready" || !token) {
      router.push(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
      return;
    }
    void toggleFavorite(token, auctionId);
  }

  return (
    <button
      type="button"
      className={`icon-button outline fav${saved ? " saved" : ""}`}
      aria-pressed={saved}
      aria-label={saved ? `Remove ${title} from saved` : `Save ${title}`}
      title={saved ? "Saved — we'll remind you before it ends" : "Save"}
      onClick={onClick}
    >
      <HeartIcon filled={saved} />
    </button>
  );
}
