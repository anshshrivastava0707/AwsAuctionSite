import type {
  Auction, AuctionPage, BidHistoryEntry, Category, ListingInput, PrivateUser, PublicUser, Snapshot, SortKey,
  UploadTicket,
} from "./types";

// Each URL is either absolute (the AWS endpoints) or a same-origin path such as
// "/api" / "/ws", which next.config.ts proxies to a local backend. Paths keep the
// browser talking only to the host that served the page, so tunnels just work.
export const PUBLIC_API_URL = process.env.NEXT_PUBLIC_API_URL ?? "";
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "";

const isPath = (url: string) => url.startsWith("/");

// Server-side fetches can't use a bare path, so they go straight to the backend.
export const API_URL =
  typeof window === "undefined" && isPath(PUBLIC_API_URL)
    ? process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000"
    : PUBLIC_API_URL;

/** The WebSocket URL to dial from the browser, resolving a path against the page's host. */
export function resolveWsUrl(token?: string | null): string {
  let url = WS_URL;
  if (isPath(url)) {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    url = `${scheme}//${window.location.host}${url}`;
  }
  // Browsers can't set headers on a WebSocket, so the login token rides in the query.
  return token ? `${url}?token=${encodeURIComponent(token)}` : url;
}

/** Image URLs end up in markup the *browser* loads, so they always use the public base. */
export const imageUrl = (key: string, size?: "thumb") => `${PUBLIC_API_URL}/images/${key}${size ? `?size=${size}` : ""}`;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Called on any 401 so the app can drop a dead session (set by lib/auth.tsx). */
let onUnauthorized: (() => void) | null = null;
export const setUnauthorizedHandler = (fn: (() => void) | null) => {
  onUnauthorized = fn;
};

async function request<T>(path: string, init: RequestInit & { token?: string | null } = {}): Promise<T> {
  if (!API_URL) throw new ApiError(0, "NEXT_PUBLIC_API_URL is not configured");
  const { token, headers, ...rest } = init;
  // no-store: a page load must reflect the persisted state right now, never a cached copy.
  const res = await fetch(`${API_URL}${path}`, {
    cache: "no-store",
    ...rest,
    headers: {
      ...(rest.body ? { "content-type": "application/json" } : {}),
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && token) onUnauthorized?.();
  if (!res.ok) throw new ApiError(res.status, body.message ?? `HTTP ${res.status}`);
  return body as T;
}

const json = (method: string, body?: unknown) => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
});

// ---------------------------------------------------------------- public

export interface AuctionQuery {
  category?: Category | null;
  sort?: SortKey;
  q?: string;
  cursor?: string | null;
  limit?: number;
}

export function auctionQueryString({ category, sort, q, cursor, limit }: AuctionQuery): string {
  const params = new URLSearchParams();
  if (category) params.set("category", category);
  if (sort) params.set("sort", sort);
  if (q) params.set("q", q);
  if (cursor) params.set("cursor", cursor);
  if (limit) params.set("limit", String(limit));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export const listAuctions = (query: AuctionQuery = {}) => request<AuctionPage>(`/auctions${auctionQueryString(query)}`);

export const getAuction = (id: string) => request<Snapshot>(`/auctions/${encodeURIComponent(id)}`);

export const getUser = (id: string) =>
  request<{ user: PublicUser }>(`/users/${encodeURIComponent(id)}`).then((r) => r.user);

export const getUserAuctions = (id: string) =>
  request<{ auctions: Auction[] }>(`/users/${encodeURIComponent(id)}/auctions`).then((r) => r.auctions);

// ---------------------------------------------------------------- session

export const devLogin = (email: string) =>
  request<{ token: string; userId: string; expiresAt: number }>("/auth/dev-login", json("POST", { email }));

export const getMe = (token: string) =>
  request<{ user: PrivateUser }>("/me", { token }).then((r) => r.user);

export const createAccount = (token: string, input: { displayName: string; requestSeller: boolean }) =>
  request<{ user: PrivateUser }>("/me", { token, ...json("POST", input) }).then((r) => r.user);

export const updateProfile = (
  token: string,
  input: Partial<{ displayName: string; bio: string; location: string; avatarKey: string | null; emailNotifications: boolean }>,
) => request<{ user: PrivateUser }>("/me", { token, ...json("PUT", input) }).then((r) => r.user);

export const requestSellerAccess = (token: string) =>
  request<{ user: PrivateUser }>("/me/seller-request", { token, ...json("POST") }).then((r) => r.user);

export const getMyBids = (token: string) =>
  request<{ entries: BidHistoryEntry[] }>("/me/bids", { token }).then((r) => r.entries);

export const getSaved = (token: string) =>
  request<{ auctions: Auction[] }>("/me/saved", { token }).then((r) => r.auctions);

export const setSaved = (token: string, auctionId: string, saved: boolean) =>
  request<{ saved: boolean }>(`/me/saved/${encodeURIComponent(auctionId)}`, { token, method: saved ? "PUT" : "DELETE" });

export const getMyAuctions = (token: string) =>
  request<{ auctions: Auction[] }>("/me/auctions", { token }).then((r) => r.auctions);

// ---------------------------------------------------------------- selling

export const createAuction = (token: string, input: ListingInput) =>
  request<{ auction: Auction }>("/auctions", { token, ...json("POST", input) }).then((r) => r.auction);

export const updateAuction = (token: string, id: string, changes: Partial<ListingInput>) =>
  request<{ auction: Auction }>(`/auctions/${encodeURIComponent(id)}`, { token, ...json("PATCH", changes) })
    .then((r) => r.auction);

export const cancelAuction = (token: string, id: string) =>
  request<{ auction: Auction }>(`/auctions/${encodeURIComponent(id)}/cancel`, { token, ...json("POST") })
    .then((r) => r.auction);

/** Uploads one image file and returns its storage key. */
export async function uploadImage(token: string, file: File): Promise<string> {
  const ticket = await request<UploadTicket>("/uploads", {
    token, ...json("POST", { contentType: file.type, size: file.size }),
  });
  // A relative ticket URL (local server) is relative to the API base.
  const url = ticket.url.startsWith("/") ? `${PUBLIC_API_URL}${ticket.url}` : ticket.url;
  let res: Response;
  if (ticket.method === "POST") {
    const form = new FormData();
    for (const [k, v] of Object.entries(ticket.fields)) form.append(k, v);
    form.append("file", file); // S3 requires the file to be the last field
    res = await fetch(url, { method: "POST", body: form });
  } else {
    res = await fetch(url, { method: "PUT", headers: ticket.headers, body: file });
  }
  if (!res.ok) throw new ApiError(res.status, `Upload failed (HTTP ${res.status})`);
  return ticket.key;
}

// ---------------------------------------------------------------- admin

export const adminRemoveAuction = (token: string, id: string, reason: string) =>
  request<{ auction: Auction }>(`/admin/auctions/${encodeURIComponent(id)}/remove`, { token, ...json("POST", { reason }) })
    .then((r) => r.auction);

export const adminListUsers = (token: string, filter: "pending" | "all") =>
  request<{ users: PrivateUser[] }>(`/admin/users?filter=${filter}`, { token }).then((r) => r.users);

export const adminSetApproval = (
  token: string,
  userId: string,
  changes: Partial<Pick<PrivateUser, "buyerStatus" | "sellerStatus">>,
) => request<{ user: PrivateUser }>(`/admin/users/${encodeURIComponent(userId)}`, { token, ...json("POST", changes) })
  .then((r) => r.user);
