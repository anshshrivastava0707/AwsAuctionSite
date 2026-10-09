# BidBloom: a real-time auction site on AWS

BidBloom is an online auction site where bids show up on every open page as soon as they're placed.
People list items, others bid on them live, and when the clock runs out the highest bidder wins and
both sides get an email. The backend is fully serverless on AWS (Lambda, API Gateway, DynamoDB,
S3, EventBridge, SES, Cognito), and the frontend is a Next.js app hosted on AWS Amplify.

```
awsproject/
├── backend/    Python 3.12 Lambdas, SAM template (backend/template.yaml), tests, local server, scripts
├── frontend/   Next.js 15 (App Router, TypeScript)
└── amplify.yml Amplify Hosting build spec for the frontend
```

---

## How the website works

### 1. Browsing (no account needed)

The home page lists live and upcoming auctions, plus any that ended in the last 15 minutes. You can
filter by category, search by keyword and sort by **Newly listed**, **Ending soon** or price. Prices
and countdowns on the cards update live, with no refresh.

Opening an auction shows its photo gallery, description, condition, quantity, seller and full bid
history. The page holds a live connection to the server, so new bids, price changes and deadline
extensions appear as they happen. Logged-out visitors can watch everything but can't bid.

### 2. Signing up and getting approved

1. **Create a login.** Sign up with an email and password. Cognito emails you a verification code,
   and password reset works the same way.
2. **Create an account.** On first login, onboarding asks for a display name and whether you also
   want to sell. Every account requests the **buyer** role, and ticking the box requests the
   **seller** role too. You can add a profile picture or request seller access later from
   **Account**.
3. **Wait for approval.** An admin reviews requests at `/admin`. You need an approved buyer role to
   bid and an approved seller role to list items. Logging in alone doesn't let you do either.

### 3. Selling an item

From **Sell**, an approved seller creates a listing with:

- a title, description, category, condition and up to 8 photos;
- a **quantity**: several identical items are sold as one lot to a single winner;
- a **starting price** and a **minimum bid increment**;
- a **start time** (now, or up to 30 days ahead) and a **duration** (up to 7 days);
- an optional secret **reserve price**: if bidding ends below it, the item doesn't sell;
- an optional **Buy it now** price that ends the auction immediately.

Photos upload straight from the browser to S3, and a background function makes small WebP
thumbnails for the cards. Until the first bid arrives, the seller can **edit** or **cancel** the
listing. After that it's locked, so no bidder ever bids on terms that later change.

**Selling** (`/me/selling`) lists everything you've put up and how each listing is doing.

### 4. Bidding

On a live auction an approved buyer has three ways to bid:

- **Place a bid** at or above the minimum next bid shown on the page.
- **Automatic bidding.** Enter the most you're willing to pay. The site bids for you, one increment
  at a time, only as high as needed to keep you in the lead. Your maximum stays private. If two
  people set automatic bids, the higher maximum wins at one increment above the other's, and when
  the maximums are equal, whoever bid first keeps the lead.
- **Buy it now**, if the seller set a price and nobody has bid yet. This ends the auction at once
  and you win.

Every bid gets an immediate answer: accepted (with whether you're leading), or rejected with a
reason such as "too low", "auction has ended" or "listing changed". Sellers can't bid on their own
items.

**Soft close.** A bid placed in the last 2 minutes pushes the end time to 2 minutes from that bid.
Last-second sniping doesn't work: everyone always gets a chance to respond.

**My bids** (`/me/bids`) shows every auction you've bid on and whether you're winning. The heart
icon **saves** an auction to `/me/saved`, and listings show how many people have saved them.

### 5. When an auction ends

At the end time a scheduled job closes the auction, and every open page sees it switch to *Ended*.
Then:

| Outcome | Who gets an email |
|---|---|
| Sold (highest bid met the reserve, or Buy it now) | One sale confirmation to the buyer, with the seller in CC, so both have the same record: a reference number, item, price and each other's email |
| Bids, but the reserve wasn't met | Seller: the item didn't sell and nobody pays anything |
| No bids | Seller: a nudge to relist it |

Bidders also get emails along the way: **outbid** when someone takes the lead from them, and
**ending soon** an hour before the end of any auction they've bid on or saved. The site doesn't take
payment. The emails connect buyer and seller, who arrange payment and delivery between themselves.

### 6. Profiles and admin

Every user has a public profile at `/users/{id}` with their picture and listings. Admins (emails
listed in the stack's `AdminEmails`) get an **Admin** page where they approve or reject buyer and
seller requests and can take down a listing that breaks the rules. The seller and the leading
bidder are emailed when a listing is taken down.

---

## How it works under the hood

### Architecture

```
                         ┌──────────── Cognito user pool (sign-up / sign-in / reset)
                         │
Browser (Next.js on Amplify)
   │
   ├─https─► API Gateway HTTP API ─► HttpFunction ──────────┬─► DynamoDB
   │          (pages, listings, accounts, uploads, admin)   │     Auctions (with stream) · Bids
   │                                                        │     Connections · Users · Saves
   │                                                        └─► S3 images (private, signed URLs)
   │                                                               │ "Object Created"
   │                                                               └─► ThumbnailFunction (WebP thumbs)
   │
   └─wss──► API Gateway WebSocket API ─► WebSocketFunction
              (subscribe · watch · placeBid · ping)     │ bids are committed to DynamoDB
                                                        ▼
                                   Auctions table stream ─┬─► BroadcastFunction ─► push to every subscriber
                                                          └─► NotifyFunction ─────► SES (outbid / sale / no sale)

EventBridge Scheduler  ── at endsAt ─────► CloseAuctionFunction (closes the auction; the stream does the rest)
                       ── 1 h before end ─► NotifyFunction ─► SES (ending soon)
```

| Piece | Role |
|---|---|
| **Amplify Hosting** | Builds and serves the Next.js app (server-rendered pages). Rebuilds on every push to `main`. |
| **HTTP API → `HttpFunction`** | All request/response calls: listings, search, accounts, saves, uploads, admin. Routing lives in `backend/src/handlers/http.py`. |
| **WebSocket API → `WebSocketFunction`** | Live connections. Clients subscribe to one auction (detail page) or watch up to 100 (card grids), and place bids over the socket. |
| **DynamoDB** | The only source of truth. Lambdas keep no state between calls. |
| **DynamoDB Stream** | Every committed change to an auction triggers the broadcaster and the email sender. |
| **EventBridge Scheduler** | One-off schedules per auction: close it at `endsAt`, send the ending-soon email an hour before. |
| **S3 + `ThumbnailFunction`** | Private image storage with signed upload and download URLs, plus automatic thumbnails. |
| **SES** | Outgoing email. |
| **Cognito** | Logins. The browser talks to Cognito directly, and the backend verifies the ID token on every call. |

### The life of a bid

1. The browser sends `placeBid` over the WebSocket with a client-generated `bidId`, the amount and
   the version of the listing terms it's bidding on.
2. `WebSocketFunction` reads the auction and works out the result in `plan_bid`, a pure function:
   automatic counter-bids, reserve, soft-close extension, Buy it now.
3. It commits that result in **one DynamoDB transaction**, conditioned on the auction's `version`
   being unchanged, the auction being open, the bidder being an approved buyer and not the seller,
   and the `bidId` not existing yet. If anything changed in between, the transaction fails, and the
   bid is re-planned against the fresh state.
4. The bidder gets a `bidResult` reply.
5. The committed change appears on the Auctions table's stream. `BroadcastFunction` pushes the new
   state to every connection watching that auction, and `NotifyFunction` emails the previous leader
   if they were outbid.

Clients only ever see committed state, and every message carries a rising `version` number, so a
browser drops stale or duplicate updates and re-syncs if it notices a gap.

### The life of an auction

```
SCHEDULED ──(startsAt passes)──► LIVE ──(endsAt passes; scheduler closes it)──► ENDED
     │                            │  (soft close may push endsAt later)           │
     └── seller cancels ──► CANCELLED (only possible before the first bid)        └─► emails sent
```

Only `OPEN`, `CLOSED` and `CANCELLED` are stored. *Scheduled*, *live* and *ended* are worked out
from the clock, so no job is needed to start an auction. When the close job runs and finds the end
time has moved because of a soft-close extension, it reschedules itself.

### Why it stays correct under pressure

| Concern | How it's handled | Code |
|---|---|---|
| **Two bids at the same moment** | Read, plan, then one `TransactWriteItems` conditioned on the version that was read: an atomic compare-and-set. The loser of a race is re-planned, never applied on stale data. Contention retries back off. | `auction/repository.py::place_bid`, `plan_bid` |
| **Automatic bidding fairness** | The leader's maximum (`proxyMax`) lives on the auction row and is never sent to clients. The outcome doesn't depend on the order bids arrive in. | `plan_bid` |
| **Seller edits during bidding** | Edit and cancel require `bidCount = 0`. Every bid carries the `termsVersion` it saw, and is rejected with `TERMS_CHANGED` if the listing changed in between. | `repository.update_auction`, `cancel_auction` |
| **Who is bidding** | The server never trusts a user id from the client. HTTP calls carry `Authorization: Bearer <token>`, and the WebSocket passes it as `?token=` at connect time, which binds the user to the connection. Buyer approval is checked **inside** the bid transaction, so revoking it takes effect at once. | `auction/auth.py`, `auction/users.py` |
| **Dropped connections** | Bids are idempotent by `bidId`. A client that disconnects mid-bid reconnects, re-sends the same bid and is told "already accepted" instead of bidding twice. Disconnecting only removes a routing row. Dead connections are pruned on `410 Gone` and by TTL. | `handlers/ws.py`, `frontend/lib/useAuctionSocket.ts` |
| **Missed updates** | `subscribe` registers the connection **before** reading the snapshot, so no update can slip in between. A heartbeat `ping` returns the current version as a backstop. | `handlers/ws.py` |
| **Fresh page loads** | The server-rendered auction page and the WebSocket snapshot both use **strongly consistent** reads. | `repository.snapshot` |

---

## Reference

### WebSocket protocol

Connect to `<WebSocketUrl>?token=<login token>` to bid. Without a token the connection can only
watch. The bidder is always the connection's user, and any `bidderId` in a message is ignored.

Client → server (`action` selects the API Gateway route):

```json
{"action":"subscribe","auctionId":"…"}
{"action":"watch","auctionIds":["…","…"]}
{"action":"placeBid","auctionId":"…","bidId":"<uuid>","amount":1500,"termsVersion":1}
{"action":"placeBid","auctionId":"…","bidId":"<uuid>","amount":1500,"maxAmount":5000,"termsVersion":1}   automatic bidding
{"action":"placeBid","auctionId":"…","bidId":"<uuid>","amount":9000,"buyNow":true,"termsVersion":1}      Buy it now (amount = the price)
{"action":"ping","auctionId":"…"}
```

Server → client:

```json
{"type":"snapshot","auction":{…},"bids":[…]}
{"type":"watching","auctions":[…]}
{"type":"auctionUpdate","auction":{…},"bid":{…}?,"bids":[…]?}        bids = every row the commit wrote, automatic bids included
{"type":"bidResult","bidId":"…","status":"ACCEPTED|REJECTED","reason":"BID_TOO_LOW|AUCTION_CLOSED|NOT_STARTED|OWN_AUCTION|TERMS_CHANGED|NOT_APPROVED|NOT_AUTHENTICATED|NOT_FOUND|DUPLICATE_ID|BUSY|ALREADY_LEADING|BUY_NOW_UNAVAILABLE|null","message":"…","duplicate":false,"auction":{…},"leading":true?,"yourMax":5000?,"extendedTo":1760000000000?}
{"type":"pong","auctionId":"…","version":7}
{"type":"error","message":"…"}
```

All money values are **integer cents**, and all times are epoch milliseconds.

### HTTP API

| Access | Endpoints |
|---|---|
| Public | `GET /auctions?category=&sort=newest\|ending\|price_low\|price_high&q=&cursor=&limit=` (a page plus `nextCursor`), `GET /auctions/{id}`, `GET /users/{id}`, `GET /users/{id}/auctions`, `GET /images/{key}?size=thumb` |
| Logged in | `GET\|POST\|PUT /me`, `POST /me/seller-request`, `GET /me/bids`, `GET /me/auctions`, `GET /me/saved`, `PUT\|DELETE /me/saved/{id}`, `POST /uploads`, `POST /auctions`, `PATCH /auctions/{id}`, `POST /auctions/{id}/cancel` |
| Admin | `GET /admin/users?filter=pending\|all`, `POST /admin/users/{id}`, `POST /admin/auctions/{id}/remove {reason}` |

See `backend/src/handlers/http.py`.

### Login modes

The `AuthMode` template parameter selects where identities come from:

- **`cognito`** (use this for anything real): the stack's user pool. The browser handles sign-up,
  sign-in and reset against Cognito (`frontend/lib/cognito.ts`). The backend verifies the ID
  token's RS256 signature against the pool's JWKS, plus `iss`, `aud`, `token_use` and `exp`, and
  requires a verified email (`backend/src/auction/auth.py`). Tokens last an hour and refresh in the
  background.
- **`dev`**: `POST /auth/dev-login {email}` returns an HMAC-signed token. There are no passwords,
  so anyone can sign in as any email. It's for local work only. The frontend uses it when no Cognito
  client id is configured.

### Demo admin account for judges

`python backend/scripts/create_demo_accounts.py --stack <stack>` creates (or resets) an approved admin
account with a known password, `judge.admin@bidbloom.demo` / `Judge-admin-2026`. Its address can't receive
mail and is never emailed. Add it to the stack's `AdminEmails` for it to have admin rights.

---

## Running it yourself

### Backend

Prerequisites: an AWS account with credentials configured, the
[AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) and the
[SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest                             # unit + handler tests (moto, offline), incl. a 40-thread bid race x5
cfn-lint template.yaml             # template lint

sam build
sam deploy --guided                # first time; prints the API URLs and Cognito ids
sam sync --watch                   # fast iteration on Lambda code against a dev stack
```

Main deploy parameters:

| Parameter | Meaning |
|---|---|
| `AuthMode` | `cognito` or `dev` (see above) |
| `AdminEmails` | Comma-separated emails that get admin rights |
| `AllowedOrigin` | The frontend's URL, for CORS |
| `SiteUrl` | The frontend's URL, used for links in emails |
| `NotifyFrom` | SES-verified sender, e.g. `BidBloom <alerts@example.com>`. If empty, emails are logged instead of sent |
| `DevAuthSecret` | Signing secret for `dev` mode. Keep it the same across deploys, or existing logins break |

Test bidding under real concurrency against a deployed stack:

```bash
python scripts/concurrency_test.py --api <HttpApiUrl> --ws <WebSocketUrl> --admin-email <an AdminEmails entry> --bidders 40 --watchers 5
```

Tear down with `sam delete`.

### Frontend

```bash
cd frontend
npm ci
cp .env.example .env.local         # fill in the URLs (and Cognito ids) from the `sam deploy` outputs
npm run dev                        # http://localhost:3000
```

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `HttpApiUrl` stack output |
| `NEXT_PUBLIC_WS_URL` | `WebSocketUrl` stack output |
| `NEXT_PUBLIC_COGNITO_USER_POOL_ID` | `UserPoolId` stack output (omit for dev login) |
| `NEXT_PUBLIC_COGNITO_CLIENT_ID` | `UserPoolClientId` stack output (omit for dev login) |

**Fully local, without AWS:** run `python backend/scripts/local_server.py` (admin login
`admin@local.test`), then start the frontend with same-origin paths. Next.js proxies `/api` and
`/ws` to the local backend (`next.config.ts`), so the whole app runs on port 3000, and a single
tunnel (e.g. `ngrok http 3000`) is enough to share it:

```bash
NEXT_PUBLIC_API_URL=/api NEXT_PUBLIC_WS_URL=/ws npm run dev
```

**Deploying:** connect the repo to **AWS Amplify Hosting**. `amplify.yml` sets the app root to
`frontend`. Add the env vars above in the Amplify console, then redeploy the backend with
`AllowedOrigin` and `SiteUrl` set to the Amplify domain.

---

## Known limits

- **Search** is a filtered Scan over `searchText`, which is fine for thousands of listings. Beyond
  that it should move to OpenSearch. Price sorts cover at most 1,000 open listings.
- **Listing pages** read one index partition. That's fine at demo scale, but it should be sharded
  for heavy traffic.
- **Emails** need an SES-verified sender. While the account is in the SES sandbox they only reach
  verified addresses. Outbid emails aren't throttled, so a long bidding war sends one per lead
  change.
- **`dev` login** lets anyone sign in as any email, so approval is only as strong as the login.
- **My bids** reads a secondary index that is eventually consistent. A bid placed a moment ago can
  take about a second to appear there. The auction page itself is always up to date.
- **Thumbnails** are created a moment after upload. Pages show the original image until then.
- **Seller names** are copied onto a listing when it's created, so renaming later doesn't change
  old listings.
- **Extreme contention** on one auction can return `BUSY` after repeated retries. The bid is
  rejected cleanly, never applied incorrectly.
- **The offline race test** runs on moto, whose in-memory DynamoDB isn't atomic across threads, so
  the test serialises moto's operations to emulate DynamoDB. The authoritative proof is
  `scripts/concurrency_test.py` against a real stack.
- **WebSocket routes:** the `Deployment` resource is static. If you add or change routes, rename
  its logical ID so CloudFormation creates a new deployment.
