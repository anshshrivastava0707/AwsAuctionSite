import { resolveWsUrl } from "./api";
import type { ServerMessage } from "./types";

/**
 * A self-healing WebSocket to the auction backend, shared by every live page.
 *
 *  - Reconnects with exponential backoff + jitter (no stampede after a backend blip),
 *    and immediately when the browser comes back online or the tab becomes visible.
 *  - Sends a heartbeat every 45 s (API Gateway drops sockets idle for 10 min) and
 *    force-reconnects a half-open socket that has gone quiet.
 *  - `onOpen` runs on every (re)connect, so callers re-subscribe there and always
 *    land on fresh, strongly consistent state after a drop.
 */
export const HEARTBEAT_MS = 45_000;
const DEAD_AFTER_MS = HEARTBEAT_MS * 2 + 10_000;

export type SocketPhase = "connecting" | "reconnecting";

export interface LiveSocket {
  /** false if the socket isn't open right now (the caller decides whether to queue). */
  send: (payload: object) => boolean;
  close: () => void;
}

export function openLiveSocket(opts: {
  token: string | null;
  onOpen: (send: LiveSocket["send"]) => void;
  onMessage: (msg: ServerMessage) => void;
  onPhase: (phase: SocketPhase) => void;
  heartbeat: () => object;
  /** Also run when the tab becomes visible again on an open socket (cheap re-sync). */
  onVisible?: (send: LiveSocket["send"]) => void;
}): LiveSocket {
  let ws: WebSocket | null = null;
  let disposed = false;
  let attempt = 0;
  let lastSeen = Date.now();
  let retryTimer: ReturnType<typeof setTimeout> | undefined;

  const send = (payload: object) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
      return true;
    }
    return false;
  };

  const connect = () => {
    if (disposed) return;
    opts.onPhase(attempt === 0 ? "connecting" : "reconnecting");
    const sock = new WebSocket(resolveWsUrl(opts.token));
    ws = sock;
    sock.onopen = () => {
      attempt = 0;
      lastSeen = Date.now();
      opts.onOpen(send);
    };
    sock.onmessage = (ev) => {
      lastSeen = Date.now();
      let msg: ServerMessage;
      try {
        msg = JSON.parse(ev.data) as ServerMessage;
      } catch {
        return; // ignore malformed frames
      }
      opts.onMessage(msg);
    };
    sock.onerror = () => sock.close();
    sock.onclose = () => {
      if (ws === sock) ws = null;
      if (disposed) return;
      opts.onPhase("reconnecting");
      const delay = Math.min(30_000, 500 * 2 ** attempt) * (0.5 + Math.random() / 2);
      attempt += 1;
      retryTimer = setTimeout(connect, delay);
    };
  };

  const reconnectNow = () => {
    if (ws && ws.readyState <= WebSocket.OPEN) return;
    clearTimeout(retryTimer);
    attempt = 0;
    connect();
  };

  const heartbeat = setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (Date.now() - lastSeen > DEAD_AFTER_MS) {
      ws.close(); // half-open socket: force the reconnect path
      return;
    }
    send(opts.heartbeat());
  }, HEARTBEAT_MS);

  const onVisibility = () => {
    if (document.visibilityState !== "visible") return;
    if (ws && ws.readyState === WebSocket.OPEN) opts.onVisible?.(send);
    else reconnectNow();
  };
  window.addEventListener("online", reconnectNow);
  document.addEventListener("visibilitychange", onVisibility);

  connect();
  return {
    send,
    close: () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearInterval(heartbeat);
      window.removeEventListener("online", reconnectNow);
      document.removeEventListener("visibilitychange", onVisibility);
      ws?.close();
      ws = null;
    },
  };
}
