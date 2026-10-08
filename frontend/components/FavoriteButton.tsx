"use client";

import { toggleFavorite, useFavorites } from "@/lib/useFavorites";
import { HeartIcon } from "./icons";

export default function FavoriteButton({ auctionId, title }: { auctionId: string; title: string }) {
  const saved = useFavorites().has(auctionId);
  return (
    <button
      type="button"
      className={`icon-button outline fav${saved ? " saved" : ""}`}
      aria-pressed={saved}
      aria-label={saved ? `Remove ${title} from saved` : `Save ${title}`}
      onClick={() => toggleFavorite(auctionId)}
    >
      <HeartIcon filled={saved} />
    </button>
  );
}
