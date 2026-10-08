"use client";

import { useEffect, useReducer } from "react";
import { WS_URL } from "./api";
import { openLiveSocket } from "./liveSocket";
import type { Auction, ServerMessage } from "./types";
import type { ConnStatus } from "./useAuctionSocket";

/**
 * Live view of many auctions on ONE socket (the "My bids" page), plus alerts for
 * the transitions a bidder cares about: outbid, won, lost, cancelled.
 *
 * Every auctionUpdate carries the full auction, so a missed broadcast can't leave
 * a gap here: the next update (or the fresh `watching` reply after a reconnect, or
 * when the tab becomes visible again) brings everything current.
 */

const MAX_WATCHED = 100; // backend connections.MAX_SUBSCRIPTIONS

export type AlertKind = "OUTBID" | "WON" | "LOST" | "RESERVE_NOT_MET" | "CANCELLED";

export interface BidAlert {
  id: string;
  kind: AlertKind;
  auctionId: string;
  title: string;
  /** For OUTBID: the new high bid. */
  amount: number | null;
  at: number;
}

interface State {
  auctions: Record<string, Auction>;
  conn: ConnStatus;
  alerts: BidAlert[];
}

type Action =
  | { kind: "conn"; conn: ConnStatus }
  | { kind: "auctions"; auctions: Auction[]; userId: string | null; live: boolean }
  | { kind: "dismiss"; id: string }
  | { kind: "reset"; auctions: Auction[] };

const isOver = (a: Auction) => a.status !== "OPEN";

function transition(prev: Auction, next: Auction, userId: string): AlertKind | null {
  if (next.status === "CANCELLED" && prev.status !== "CANCELLED") return "CANCELLED";
  if (next.status === "CLOSED" && prev.status !== "CLOSED") {
    if (!next.reserveMet) return "RESERVE_NOT_MET";
    return next.highBidderId === userId ? "WON" : "LOST";
  }
  if (!isOver(next) && prev.highBidderId === userId && next.highBidderId !== userId) return "OUTBID";
  return null;
}

function reducer(state: State, action: Action): State {
  switch (action.kind) {
    case "conn":
      return { ...state, conn: action.conn };
    case "reset":
      return { ...state, auctions: Object.fromEntries(action.auctions.map((a) => [a.auctionId, a])) };
    case "dismiss":
      return { ...state, alerts: state.alerts.filter((a) => a.id !== action.id) };
    case "auctions": {
      const auctions = { ...state.auctions };
      const alerts = [...state.alerts];
      for (const next of action.auctions) {
        const prev = auctions[next.auctionId];
        if (!prev || next.version <= prev.version) continue; // only auctions we show; never go backwards
        auctions[next.auctionId] = next;
        const kind = action.userId ? transition(prev, next, action.userId) : null;
        if (kind) {
          // One alert per auction: a newer event replaces an older unread one.
          const i = alerts.findIndex((a) => a.auctionId === next.auctionId);
          if (i >= 0) alerts.splice(i, 1);
          alerts.unshift({
            id: `${next.auctionId}:${next.version}`,
            kind,
            auctionId: next.auctionId,
            title: next.title,
            amount: next.currentHigh,
            at: Date.now(),
          });
        }
      }
      return { ...state, auctions, alerts, conn: action.live ? "live" : state.conn };
    }
  }
}

export function useWatchAuctions(initial: Auction[], userId: string | null, token: string | null, ready = true) {
  const [state, dispatch] = useReducer(reducer, null, () => ({
    auctions: Object.fromEntries(initial.map((a) => [a.auctionId, a])),
    conn: (WS_URL ? "connecting" : "unconfigured") as ConnStatus,
    alerts: [],
  }));

  useEffect(() => dispatch({ kind: "reset", auctions: initial }), [initial]);

  // Only auctions that can still change are worth a subscription.
  const watchIds = initial.filter((a) => !isOver(a)).map((a) => a.auctionId).slice(0, MAX_WATCHED);
  const watchKey = watchIds.join(",");

  useEffect(() => {
    if (!WS_URL || !ready) return;
    const ids = watchKey ? watchKey.split(",") : [];
    if (ids.length === 0) {
      dispatch({ kind: "conn", conn: "live" }); // nothing can change; no socket needed
      return;
    }
    const watch = (send: (p: object) => boolean) => send({ action: "watch", auctionIds: ids });
    const socket = openLiveSocket({
      token,
      onOpen: watch,
      onVisible: watch,
      onPhase: (conn) => dispatch({ kind: "conn", conn }),
      heartbeat: () => ({ action: "ping" }),
      onMessage: (msg: ServerMessage) => {
        if (msg.type === "watching") dispatch({ kind: "auctions", auctions: msg.auctions, userId, live: true });
        if (msg.type === "auctionUpdate") dispatch({ kind: "auctions", auctions: [msg.auction], userId, live: false });
      },
    });
    return () => socket.close();
  }, [watchKey, token, userId, ready]);

  return {
    auctions: state.auctions,
    conn: state.conn,
    alerts: state.alerts,
    dismiss: (id: string) => dispatch({ kind: "dismiss", id }),
  };
}
