import { networkInterfaces } from "node:os";
import type { NextConfig } from "next";

// Where this Next.js server reaches the backend when the browser-facing URLs are
// same-origin paths (NEXT_PUBLIC_API_URL=/api, NEXT_PUBLIC_WS_URL=/ws). Proxying
// both through Next means the whole app is served from one port, so a single
// tunnel (ngrok etc.) or LAN address works for everyone.
const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";
const WS_INTERNAL_URL = process.env.WS_INTERNAL_URL ?? "http://127.0.0.1:8001";

// `next dev` blocks its JS bundles for any host not listed here, which leaves the
// page un-hydrated (no WebSocket, dead Bid button). Allow common tunnels, this
// machine's own LAN addresses, and anything in DEV_ORIGINS (comma-separated).
// Production (`next build && next start`) does not apply this check.
const lanAddresses = Object.values(networkInterfaces())
  .flat()
  .filter((i) => i && i.family === "IPv4" && !i.internal)
  .map((i) => i!.address);

const allowedDevOrigins = [
  "*.ngrok-free.app", "*.ngrok.app", "*.ngrok.io",
  "*.trycloudflare.com", "*.loca.lt",
  "127.0.0.1", ...lanAddresses,
  ...(process.env.DEV_ORIGINS ?? "").split(",").map((s) => s.trim()).filter(Boolean),
];

const nextConfig: NextConfig = {
  allowedDevOrigins,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_INTERNAL_URL}/:path*` },
      // WebSocket upgrades on /ws are proxied to the backend's WebSocket server.
      { source: "/ws", destination: `${WS_INTERNAL_URL}/` },
    ];
  },
};

export default nextConfig;
