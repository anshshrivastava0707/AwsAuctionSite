# Frontend (Next.js)

See the root `../README.md` for setup, env vars and deployment.

- `app/auctions/[id]/page.tsx` — server-renders the auction from a strongly consistent read
- `lib/useAuctionSocket.ts` — WebSocket client: version-ordered updates, gap re-sync, reconnect with backoff, heartbeat, idempotent re-send of unacknowledged bids
- `lib/types.ts` — mirrors `backend/src/auction/models.py`
