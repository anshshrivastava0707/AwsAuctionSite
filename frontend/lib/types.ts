// Mirrors backend/src/auction/models.py (public_auction / public_bid / public_user /
// private_user) and the WebSocket protocol in the root README. All money is integer
// cents; all times are epoch milliseconds.

export const CATEGORIES = {
  ELECTRONICS: "Electronics",
  COLLECTIBLES: "Collectibles",
  FASHION: "Fashion",
  HOME_GARDEN: "Home & garden",
  ART: "Art",
  SPORTS: "Sports",
  TOYS: "Toys",
  VEHICLES: "Vehicles",
  BOOKS: "Books",
  OTHER: "Other",
} as const;
export type Category = keyof typeof CATEGORIES;

export const CONDITIONS = {
  NEW: "New",
  LIKE_NEW: "Like new",
  GOOD: "Good",
  FAIR: "Fair",
  FOR_PARTS: "For parts / not working",
} as const;
export type Condition = keyof typeof CONDITIONS;

/** Derived from status + clock on the server; the UI re-derives it live (lib/format.ts). */
export type Phase = "SCHEDULED" | "LIVE" | "ENDED" | "CANCELLED";

export interface Auction {
  auctionId: string;
  sellerId: string | null;
  sellerName: string | null;
  title: string;
  description: string;
  category: Category;
  condition: Condition;
  quantity: number;
  images: string[];
  startingPrice: number;
  minIncrement: number;
  minNextBid: number;
  currentHigh: number | null;
  highBidderId: string | null;
  highBidderName: string | null;
  bidCount: number;
  status: "OPEN" | "CLOSED" | "CANCELLED";
  phase: Phase;
  isOpen: boolean;
  version: number;
  termsVersion: number;
  createdAt: number;
  updatedAt: number;
  startsAt: number;
  endsAt: number;
}

export interface Bid {
  bidId?: string; // absent on bids that arrive via broadcast
  auctionId: string;
  bidderId: string;
  bidderName: string;
  amount: number;
  placedAt: number;
}

export interface Snapshot {
  auction: Auction;
  bids: Bid[];
}

export type ApprovalStatus = "NONE" | "PENDING" | "APPROVED" | "REJECTED";

export interface PublicUser {
  userId: string;
  displayName: string;
  bio: string;
  location: string;
  avatarKey: string | null;
  isSeller: boolean;
  createdAt: number;
}

export interface PrivateUser extends PublicUser {
  email: string;
  buyerStatus: ApprovalStatus;
  sellerStatus: ApprovalStatus;
  isAdmin: boolean;
}

export interface BidHistoryEntry {
  auction: Auction;
  bids: Bid[]; // my bids on this auction, newest first
}

export type ListingInput = {
  title: string;
  description: string;
  category: Category;
  condition: Condition;
  quantity: number;
  images: string[];
  startingPrice: number;
  minIncrement: number;
  startsAt: number;
  endsAt: number;
};

export type UploadTicket =
  | { key: string; method: "POST"; url: string; fields: Record<string, string> }
  | { key: string; method: "PUT"; url: string; headers: Record<string, string> };

export type RejectReason =
  | "BID_TOO_LOW"
  | "AUCTION_CLOSED"
  | "NOT_FOUND"
  | "DUPLICATE_ID"
  | "BUSY"
  | "STALE"
  | "NOT_STARTED"
  | "OWN_AUCTION"
  | "TERMS_CHANGED"
  | "NOT_APPROVED"
  | "NOT_AUTHENTICATED";

export type ServerMessage =
  | ({ type: "snapshot" } & Snapshot)
  | { type: "auctionUpdate"; auction: Auction; bid?: Bid }
  | {
      type: "bidResult";
      bidId: string;
      status: "ACCEPTED" | "REJECTED";
      reason: RejectReason | null;
      message: string;
      duplicate: boolean;
      auction: Auction | null;
      bid: Bid | null;
    }
  | { type: "watching"; auctions: Auction[] }
  | { type: "pong"; auctionId?: string; version?: number }
  | { type: "error"; message: string };

export interface PlaceBidMessage {
  action: "placeBid";
  auctionId: string;
  bidId: string;
  amount: number;
  termsVersion: number;
}
