"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import { WS_URL } from "./api";
import { openLiveSocket, type LiveSocket } from "./liveSocket";
import type { Auction, Bid, PlaceBidMessage, ServerMessage, Snapshot } from "./types";
import { uuid } from "./uuid";

/**
 * Live auction state over the API Gateway WebSocket.
 *
 * Consistency rules (the server is the only source of truth):
 *  - Every auction message carries `version`. Anything not newer than what we
 *    hold is ignored, so duplicate or reordered broadcasts can't move state backwards.
 *  - A version gap (we missed a broadcast) or a `pong` reporting a newer version
 *    triggers a re-subscribe, which returns a fresh strongly-consistent snapshot.
 *  - On every (re)connect we re-subscribe and get a snapshot, so reconnecting
 *    always lands on the current persisted state.
 *  - Bids not yet acknowledged are kept (sessionStorage, keyed by bidId) and
 *    re-sent after reconnecting. The server dedupes by bidId, so a bid that
 *    actually committed before the drop is reported "already accepted", never applied twice.
 *  - The socket is opened with the login token (or none: watching is public). The
 *    server takes the bidder's identity from the connection, so logging in or out
 *    reconnects, and queued bids are stored per user so they can never be re-sent
 *    under someone else's login.
 *  - Each bid carries the listing's `termsVersion`; if the seller edited the listing
 *    since, the server rejects it with TERMS_CHANGED instead of applying it.
 */

export type ConnStatus = "connecting" | "live" | "reconnecting" | "unconfigured";
export type BidResult = Extract<ServerMessage, { type: "bidResult" }>;

interface State {
  auction: Auction;
  bids: Bid[];
  conn: ConnStatus;
  lastResult: BidResult | null;
  pendingCount: number;
  error: string | null;
}

type Action =
  | { kind: "conn"; conn: ConnStatus }
  | { kind: "message"; msg: ServerMessage }
  | { kind: "pending"; count: number };

const MAX_BIDS_SHOWN = 25;

function mergeBids(current: Bid[], incoming: Bid[]): Bid[] {
  // Accepted bids have strictly increasing, therefore unique, amounts.
  const byAmount = new Map(current.map((b) => [b.amount, b]));
  for (const b of incoming) byAmount.set(b.amount, { ...byAmount.get(b.amount), ...b });
  return [...byAmount.values()].sort((a, b) => b.amount - a.amount).slice(0, MAX_BIDS_SHOWN);
}

function reducer(state: State, action: Action): State {
  switch (action.kind) {
    case "conn":
      return { ...state, conn: action.conn };
    case "pending":
      return { ...state, pendingCount: action.count };
    case "message": {
      const msg = action.msg;
      switch (msg.type) {
        case "snapshot":
          // A snapshot is authoritative for its version and replaces history.
          if (msg.auction.version >= state.auction.version) {
            return { ...state, auction: msg.auction, bids: mergeBids([], msg.bids), error: null };
          }
          return { ...state, bids: mergeBids(state.bids, msg.bids) };
        case "auctionUpdate":
          if (msg.auction.version <= state.auction.version) return state; // stale / duplicate
          return {
            ...state,
            auction: msg.auction,
            bids: msg.bid ? mergeBids(state.bids, [msg.bid]) : state.bids,
          };
        case "bidResult": {
          const newer = msg.auction && msg.auction.version > state.auction.version;
          return {
            ...state,
            lastResult: msg,
            auction: newer ? msg.auction! : state.auction,
            bids: msg.bid ? mergeBids(state.bids, [msg.bid]) : state.bids,
          };
        }
        case "error":
          return { ...state, error: msg.message };
        default:
          return state;
      }
    }
  }
}

function pendingKey(auctionId: string, userId: string | null) {
  return `auction.pending.${userId ?? "anon"}.${auctionId}`;
}

function loadPending(key: string): Map<string, PlaceBidMessage> {
  try {
    const raw = sessionStorage.getItem(key);
    return new Map(raw ? (JSON.parse(raw) as [string, PlaceBidMessage][]) : []);
  } catch {
    return new Map();
  }
}

function savePending(key: string, pending: Map<string, PlaceBidMessage>) {
  try {
    sessionStorage.setItem(key, JSON.stringify([...pending]));
  } catch {
    /* non-fatal */
  }
}

/** `ready` = the session has loaded; until then we don't know which identity to connect as. */
export function useAuctionSocket(initial: Snapshot, token: string | null, userId: string | null, ready = true) {
  const auctionId = initial.auction.auctionId;
  const storeKey = pendingKey(auctionId, userId);
  const [state, dispatch] = useReducer(reducer, {
    auction: initial.auction,
    bids: mergeBids([], initial.bids),
    conn: WS_URL ? "connecting" : "unconfigured",
    lastResult: null,
    pendingCount: 0,
    error: WS_URL ? null : "NEXT_PUBLIC_WS_URL is not configured",
  });

  const socketRef = useRef<LiveSocket | null>(null);
  const versionRef = useRef(initial.auction.version);
  const pendingRef = useRef<Map<string, PlaceBidMessage>>(new Map());
  const termsRef = useRef(initial.auction.termsVersion);
  termsRef.current = state.auction.termsVersion;

  const send = useCallback((payload: object) => socketRef.current?.send(payload) ?? false, []);

  const syncPending = useCallback(() => {
    savePending(storeKey, pendingRef.current);
    dispatch({ kind: "pending", count: pendingRef.current.size });
  }, [storeKey]);

  useEffect(() => {
    pendingRef.current = loadPending(storeKey);
    syncPending();
    if (!WS_URL || !ready) return;

    const resubscribe = () => send({ action: "subscribe", auctionId });

    const handle = (msg: ServerMessage) => {
      switch (msg.type) {
        case "snapshot":
          versionRef.current = Math.max(versionRef.current, msg.auction.version);
          dispatch({ kind: "conn", conn: "live" });
          // Re-send anything unacknowledged (idempotent on the server).
          for (const bid of pendingRef.current.values()) send(bid);
          break;
        case "auctionUpdate":
          if (msg.auction.auctionId !== auctionId) return;
          if (msg.auction.version > versionRef.current + 1) resubscribe(); // missed something
          versionRef.current = Math.max(versionRef.current, msg.auction.version);
          break;
        case "bidResult":
          pendingRef.current.delete(msg.bidId);
          syncPending();
          if (msg.auction && msg.auction.version > versionRef.current + 1) resubscribe();
          if (msg.auction) versionRef.current = Math.max(versionRef.current, msg.auction.version);
          break;
        case "pong":
          if (msg.version != null && msg.version > versionRef.current) resubscribe();
          break;
      }
      dispatch({ kind: "message", msg });
    };

    const socket = openLiveSocket({
      token,
      onOpen: (sendNow) => sendNow({ action: "subscribe", auctionId }),
      onMessage: handle,
      onPhase: (conn) => dispatch({ kind: "conn", conn }),
      heartbeat: () => ({ action: "ping", auctionId }),
    });
    socketRef.current = socket;
    return () => {
      socket.close();
      if (socketRef.current === socket) socketRef.current = null;
    };
  }, [auctionId, send, syncPending, storeKey, token, ready]);

  const placeBid = useCallback(
    (amount: number) => {
      const msg: PlaceBidMessage = {
        action: "placeBid",
        auctionId,
        bidId: uuid(),
        amount,
        termsVersion: termsRef.current,
      };
      pendingRef.current.set(msg.bidId, msg);
      syncPending();
      // If the socket is down this stays pending and is sent after re-subscribing.
      send(msg);
      return msg.bidId;
    },
    [auctionId, send, syncPending],
  );

  return { ...state, placeBid };
}
