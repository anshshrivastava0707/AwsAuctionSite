# Real-time auction — AWS serverless backend + Next.js frontend

```
awsproject/
├── backend/    Python 3.12 Lambdas, SAM template, tests, local server, load-test script
└── frontend/   Next.js 15 (App Router, TypeScript)
```

Features: real accounts (Amazon Cognito: sign-up, email verification, password reset) with
admin-approved buyer and seller roles, profiles with pictures, auction listings with photos,
category, condition, quantity (sold as one lot to a single winner), start/end times, an optional
secret reserve price and an optional Buy it now price, edit or cancel before the first bid, live
bidding with automatic (proxy) bidding up to a private maximum, soft close (a bid in the last 2
minutes extends the auction), saved auctions with watcher counts, email notifications (outbid,
won/sold, ending soon), server-side search, sorting and paging, image thumbnails, bidding
history, and "items I'm selling". Logged-out visitors can browse and watch auctions live, but
can't bid.

## Architecture

```
Browser ──wss──► API Gateway WebSocket API ──► WebSocketFunction ($connect/$disconnect/subscribe/placeBid/ping)
   │                                                 │
   └──https──► API Gateway HTTP API ──► HttpFunction  │  (ANY /{proxy+}: routing in handlers/http.py)
                                          │  │        ▼
                                          │  │   DynamoDB: Auctions (stream) · Bids · Connections · Users · Saves
                                          │  └─► S3 (images, private; signed upload/download URLs)
                                          │       │ EventBridge "Object Created" ─► ThumbnailFunction (WebP thumbs)
                                          │       │
                         EventBridge Scheduler    └─ Auctions stream ─┬─► BroadcastFunction ─► postToConnection (all subscribers)
                         close-<id> at endsAt ─► CloseAuctionFunction └─► NotifyFunction ─► SES (outbid / won / sold)
                         remind-<id> 1h before ─► NotifyFunction ─► SES (ending soon)

Browser ──https──► Cognito user pool (sign-up / sign-in / reset); the backend verifies the ID token.
```

## How each requirement is met

| Requirement | Mechanism | Code |
|---|---|---|
| **Live push** | API Gateway WebSocket. Every committed change to an auction flows through the DynamoDB Stream to `BroadcastFunction`, which pushes it to all subscribers. Clients are only told about **committed** state. | `handlers/stream.py`, `auction/connections.py` |
| **Race-safe bids** | A bid is read → plan → one `TransactWriteItems`. `plan_bid` (a pure function) works out the outcome — automatic bids, reserve, soft-close extension, Buy it now — and the transaction commits it conditioned on `version = <the version planned from> AND status = OPEN AND startsAt <= now < endsAt`, together with the bid rows (`attribute_not_exists(bidId)`). `version` moves on every bid, edit and close, so read and write form one atomic compare-and-set: if anything changed in between, the write fails and the bid is re-planned against the state DynamoDB returns with the failure. A stale or lower bid is re-planned into a rejection. `TransactionConflict` cancellations are retried with backoff. Ties: the earlier bidder keeps the lead. | `auction/repository.py::place_bid`, `plan_bid` |
| **Automatic bidding** | The leader's maximum lives on the auction row as `proxyMax`, never sent to clients. A challenger with a higher maximum takes the lead one increment above the old maximum; otherwise the leader's automatic bid answers one increment above the challenger. The outcome doesn't depend on bidding order. | `plan_bid` |
| **Soft close** | A bid with less than 2 minutes left moves `endsAt` to now + 2 minutes, in the same transaction. The close job re-schedules itself when it finds the end has moved. | `plan_bid`, `handlers/scheduler.py` |
| **Persisted state** | DynamoDB is the only source of truth; Lambdas are stateless. A fresh page load (Next.js server render → `GET /auctions/{id}`) and a WebSocket `subscribe` both do a **strongly consistent** read. | `repository.snapshot` |
| **Identity & approval** | The backend never trusts a user id sent by the client. HTTP calls carry `Authorization: Bearer <token>`, and the WebSocket gets the token as `?token=` at `$connect`, which binds the identity to the connection. Bids use that identity. Buyer approval is a `ConditionCheck` **inside** the bid transaction, so revoking it takes effect atomically. The token source is pluggable (`auction/auth.py`). | `auction/auth.py`, `auction/users.py` |
| **Seller rules** | Sellers can't bid on their own auctions (`sellerId <> bidder` in the bid condition). Edit and cancel are conditional on `bidCount = 0`, so an edit and a first bid can never both succeed. Each bid also carries the `termsVersion` it was placed against, and an edit in between rejects it with `TERMS_CHANGED`. | `repository.update_auction`, `cancel_auction` |
| **Disconnect / reconnect safety** | `$disconnect` deletes only a routing row and never touches auction data. Dead connections are pruned on 410 Gone and by TTL. Bids are idempotent by client-generated `bidId`: a client that drops mid-bid reconnects and re-sends the same bid, and is told "already accepted" instead of bidding twice. Subscribe registers **before** reading the snapshot, so no update falls in between. Every message carries a monotonically increasing `version`: clients drop old or duplicate updates and re-sync on a gap. A heartbeat `ping` returns the current version as a backstop check. | `handlers/ws.py`, `frontend/lib/useAuctionSocket.ts` |

## WebSocket protocol

Connect to `<WebSocketUrl>?token=<login token>` to bid. Without a token, the connection can only watch.
The bidder is always the connection's user; any `bidderId` in a message is ignored.

Client → server (`action` selects the API Gateway route):

```json
{"action":"subscribe","auctionId":"…"}
{"action":"placeBid","auctionId":"…","bidId":"<uuid>","amount":1500,"termsVersion":1}
{"action":"placeBid","auctionId":"…","bidId":"<uuid>","amount":1500,"maxAmount":5000,"termsVersion":1}   automatic bidding
{"action":"placeBid","auctionId":"…","bidId":"<uuid>","amount":9000,"buyNow":true,"termsVersion":1}      Buy it now (amount = the price)
{"action":"ping","auctionId":"…"}
```

Server → client:

```json
{"type":"snapshot","auction":{…},"bids":[…]}
{"type":"auctionUpdate","auction":{…},"bid":{…}?,"bids":[…]?}        bids = every row the commit wrote, automatic bids included
{"type":"bidResult","bidId":"…","status":"ACCEPTED|REJECTED","reason":"BID_TOO_LOW|AUCTION_CLOSED|NOT_STARTED|OWN_AUCTION|TERMS_CHANGED|NOT_APPROVED|NOT_AUTHENTICATED|NOT_FOUND|DUPLICATE_ID|BUSY|ALREADY_LEADING|BUY_NOW_UNAVAILABLE|null","message":"…","duplicate":false,"auction":{…},"leading":true?,"yourMax":5000?,"extendedTo":1760000000000?}
{"type":"pong","auctionId":"…","version":7}
{"type":"error","message":"…"}
```

All money values are **integer cents**; all times are epoch milliseconds.

## Accounts and login

`AUTH_MODE` (a template parameter) selects where identities come from:

- **`cognito`**: the stack's user pool. The browser calls Cognito directly for sign-up (with an emailed
  code), sign-in and password reset (`frontend/lib/cognito.ts`); the backend verifies the ID token's RS256
  signature against the pool's JWKS plus `iss`/`aud`/`token_use`/`exp`, and requires a verified email
  (`backend/src/auction/auth.py`). ID tokens last an hour and are refreshed in the background.
  Frontend env: `NEXT_PUBLIC_COGNITO_USER_POOL_ID` and `NEXT_PUBLIC_COGNITO_CLIENT_ID` (stack outputs).
- **`dev`**: `POST /auth/dev-login {email}` returns an HMAC-signed token. There are no passwords, so anyone
  can sign in as any email. Use it only for local work and demos. The frontend uses it when no Cognito
  client id is set.

Approval is separate from login. After first login a user creates an account (`POST /me`), and an admin
(an email in `AdminEmails`) approves each role at `/admin`. Buyer status `APPROVED` is needed to bid; seller
status `APPROVED` is needed to list. Admins are approved for both roles automatically.

### Demo admin account for judges

`python backend/scripts/create_demo_accounts.py --stack <stack>` creates (or resets) an approved admin
account with a known password, `judge.admin@bidbloom.demo` / `Judge-admin-2026`. Its address can't receive
mail and is never emailed. Add it to the stack's `AdminEmails` for it to have admin rights.

## HTTP API

Public: `GET /auctions?category=&sort=newest|ending|price_low|price_high&q=&cursor=&limit=` (a page plus
`nextCursor`), `GET /auctions/{id}`, `GET /users/{id}`, `GET /users/{id}/auctions`, `GET /images/{key}?size=thumb`.
Logged in: `GET|POST|PUT /me`, `POST /me/seller-request`, `GET /me/bids`, `GET /me/auctions`,
`GET /me/saved`, `PUT|DELETE /me/saved/{id}`, `POST /uploads`, `POST /auctions`, `PATCH /auctions/{id}`,
`POST /auctions/{id}/cancel`.
Admin: `GET /admin/users?filter=pending|all`, `POST /admin/users/{id}`, `POST /admin/auctions/{id}/remove {reason}`
(take down a listing that breaks the terms, in any state but cancelled; the seller and leading bidder are emailed). See `backend/src/handlers/http.py`.

## Backend

Prerequisites: an AWS account with credentials configured (`aws configure` or SSO), the
[AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), and the
[SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).

```bash
cd backend
source .venv/bin/activate          # already created; deps from requirements-dev.txt
pytest                             # unit + handler tests (moto, offline), incl. a 40-thread bid race x5
cfn-lint template.yaml             # template lint

sam build
sam deploy --guided                # first time; set AuthMode=cognito, AdminEmails, SiteUrl, NotifyFrom; prints the URLs
sam sync --watch                   # fast iteration on Lambda code against the dev stack
```

Prove concurrency against the real deployment:

```bash
python scripts/concurrency_test.py --api <HttpApiUrl> --ws <WebSocketUrl> --admin-email <an AdminEmails entry> --bidders 40 --watchers 5
```

Tear down: `sam delete`.

## Frontend

Node is installed user-locally at `~/.local/node` (add `export PATH=$HOME/.local/node/bin:$PATH` to your shell).

```bash
cd frontend
cp .env.example .env.local         # fill in the two URLs from `sam deploy` outputs
npm run dev                        # http://localhost:3000
```

Against the local backend (`python scripts/local_server.py`, admin login `admin@local.test`), use same-origin paths so the
whole app is served from port 3000. Next.js proxies `/api` and `/ws` to the backend
(`next.config.ts`), so one tunnel (e.g. `ngrok http 3000`) is enough to share it:

```bash
NEXT_PUBLIC_API_URL=/api NEXT_PUBLIC_WS_URL=/ws npm run dev
```

Deploy: connect the repo to **AWS Amplify Hosting** (app root `frontend`, same two env vars). Then
redeploy the backend with `--parameter-overrides AllowedOrigin=https://<your-amplify-domain>`.

## Known limits / next steps
- The offline concurrency test runs on moto. moto's in-process DynamoDB is **not** atomic across threads (without help it lost an update in testing), so the test serialises moto's backend operations to emulate DynamoDB's per-request atomicity. The authoritative concurrency proof is `scripts/concurrency_test.py` against the real stack.
- In `dev` auth mode anyone can log in as any email, so approval is only as strong as the login. Use `cognito` for anything real.
- Emails need an SES-verified sender in `NotifyFrom`; while the account is in the SES sandbox they only reach verified addresses. Without `NotifyFrom` they are logged, not sent. Outbid emails aren't throttled, so a long bidding war sends one per lead change.
- Search is a filtered Scan over `searchText` (fine for thousands of listings; move to OpenSearch beyond that), and price sorts work over at most 1,000 open listings.
- Browse pages (home, sorts, search) show live and upcoming auctions plus those that ended in the last 15 minutes; older ones stay reachable from their own page, the seller's profile, My bids and Selling.
- Thumbnails are made by `ThumbnailFunction` a moment after upload; pages fall back to the original image until then (and for images uploaded before thumbnails existed).
- A login token is checked when the WebSocket connects; a connection opened before approval was revoked still bids as that user, but the bid's `ConditionCheck` rejects it.
- `GET /me/bids` reads a global secondary index, which is eventually consistent: a bid placed a moment ago can take about a second to appear.
- A listing stores the seller's display name at creation; renaming later doesn't update old listings.
- `GET /auctions` reads one index partition (`listing = "ALL"`), which is fine at demo scale; shard it for heavy traffic. Auctions created before this version have no `listing` or `sellerId` and don't appear in lists.
- Admin user lists use a filtered Scan of the Users table (admin-only and small).
- Under extreme contention on one auction, a bid can come back `BUSY` after 8 conflict retries (or 60 re-plans). It is rejected cleanly, never applied incorrectly.
- Upgrading an existing stack: auctions created before the `byEnding` index have no `openListing`, so they don't show under "Ending soon" or the price sorts (they still show under "Newly listed").
- The WebSocket `Deployment` resource is static. If you add or change routes, rename its logical ID so CloudFormation creates a new deployment.
