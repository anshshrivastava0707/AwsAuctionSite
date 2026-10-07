"""End-to-end concurrency test against the DEPLOYED stack (real API Gateway,
Lambda and DynamoDB).

  python scripts/concurrency_test.py --api https://xxx.execute-api.REGION.amazonaws.com \
                                     --ws  wss://yyy.execute-api.REGION.amazonaws.com/dev \
                                     --admin-email you@example.com --bidders 30 --watchers 5

Requires AUTH_MODE=dev on the stack and --admin-email listed in its AdminEmails.

What it does:
  0. Logs in as the admin, creates N bidder accounts and approves them.
  1. Creates a fresh auction over HTTP (the admin is the seller).
  2. Opens N bidder sockets + M watcher sockets, all subscribed.
  3. Every bidder fires a bid at the same instant (asyncio barrier); many share
     the same amount to force ties and transaction conflicts.
  4. Drops and reconnects some bidders mid-flight, re-sending the same bidId.
  5. Verifies, from a fresh HTTP read (what a new page load sees):
       - final high == highest accepted amount == highest submitted amount
       - accepted amounts strictly increase; no two accepted bids share an amount
       - version == 1 + accepted count; bidCount == accepted count
       - every watcher converged on the final version via broadcasts
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import urllib.error
import urllib.request
import uuid

import websockets


def http_json(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def login(api: str, email: str, name: str) -> dict:
    """Dev login + create the account if it doesn't exist yet."""
    session = http_json("POST", f"{api}/auth/dev-login", {"email": email})
    try:
        http_json("POST", f"{api}/me", {"displayName": name}, session["token"])
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = account already exists from an earlier run
            raise
    return session


async def recv_until(sock, predicate, timeout=20.0):
    async def loop():
        while True:
            msg = json.loads(await sock.recv())
            if predicate(msg):
                return msg
    return await asyncio.wait_for(loop(), timeout)


async def open_subscribed(ws_url: str, auction_id: str, token: str | None = None):
    sock = await websockets.connect(f"{ws_url}?token={token}" if token else ws_url)
    await sock.send(json.dumps({"action": "subscribe", "auctionId": auction_id}))
    await recv_until(sock, lambda m: m.get("type") == "snapshot")
    return sock


async def bidder(i, ws_url, auction_id, amount, start: asyncio.Event, drop: bool, token: str):
    bid = {"action": "placeBid", "auctionId": auction_id, "bidId": uuid.uuid4().hex, "amount": amount}
    sock = await open_subscribed(ws_url, auction_id, token)
    await start.wait()
    await sock.send(json.dumps(bid))
    if drop:
        # Simulate a flaky network: kill the socket before reading the reply,
        # reconnect, and re-send the identical bid.
        await sock.close()
        sock = await open_subscribed(ws_url, auction_id, token)
        await sock.send(json.dumps(bid))
    result = await recv_until(sock, lambda m: m.get("type") == "bidResult" and m["bidId"] == bid["bidId"])
    await sock.close()
    return amount, result, drop


async def watcher(ws_url, auction_id, versions: list, stop: asyncio.Event):
    sock = await open_subscribed(ws_url, auction_id)
    try:
        while not stop.is_set():
            try:
                msg = json.loads(await asyncio.wait_for(sock.recv(), 0.5))
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == "auctionUpdate":
                versions.append(msg["auction"]["version"])
    finally:
        await sock.close()


async def main(args) -> int:
    admin = login(args.api, args.admin_email, "Load test admin")
    tokens = []
    for i in range(args.bidders):
        session = login(args.api, f"loadtest-{i}@example.com", f"Bot {i}")
        http_json("POST", f"{args.api}/admin/users/{session['userId']}", {"buyerStatus": "APPROVED"}, admin["token"])
        tokens.append(session["token"])
    print(f"{args.bidders} approved bidder accounts ready")

    created = http_json("POST", f"{args.api}/auctions", {
        "title": "Load test", "startingPrice": 1000, "minIncrement": 100, "durationSeconds": 300}, admin["token"])
    auction_id = created["auction"]["auctionId"]
    print(f"auction {auction_id}")

    # Deliberately overlapping amounts: ties + near-ties force conflicts.
    amounts = [1000 + 100 * random.randint(0, args.bidders // 2) for _ in range(args.bidders)]
    start, stop = asyncio.Event(), asyncio.Event()
    watcher_versions = [[] for _ in range(args.watchers)]
    watchers = [asyncio.create_task(watcher(args.ws, auction_id, v, stop)) for v in watcher_versions]
    tasks = [asyncio.create_task(bidder(i, args.ws, auction_id, a, start, drop=(i % 7 == 0), token=tokens[i]))
             for i, a in enumerate(amounts)]
    await asyncio.sleep(3)  # let everyone connect + subscribe
    start.set()
    results = await asyncio.gather(*tasks)
    await asyncio.sleep(3)  # let broadcasts drain
    stop.set()
    await asyncio.gather(*watchers)

    final = http_json("GET", f"{args.api}/auctions/{auction_id}")["auction"]
    # Each bidder sends one unique bid. A "duplicate" reply means that bid's first send (before the
    # simulated drop) was accepted, so it counts as accepted exactly once.
    accepted = sorted(a for a, r, _ in results if r["status"] == "ACCEPTED")
    reasons: dict = {}
    for _, r, _ in results:
        reasons[r["reason"] or "ACCEPTED"] = reasons.get(r["reason"] or "ACCEPTED", 0) + 1

    checks = {
        "final high is highest submitted": final["currentHigh"] == max(amounts),
        "final high is highest accepted": final["currentHigh"] == accepted[-1],
        "accepted amounts strictly increase": all(b > a for a, b in zip(accepted, accepted[1:])),
        "version == 1 + accepted": final["version"] == 1 + len(accepted),
        "bidCount == accepted": final["bidCount"] == len(accepted),
        "all watchers saw final version": all(v and max(v) == final["version"] for v in watcher_versions),
    }
    print(f"bids={len(results)} outcomes={reasons} dropped+resent={sum(d for *_, d in results)}")
    print(f"final: high={final['currentHigh']} by {final['highBidderName']} version={final['version']}")
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--api", required=True)
    p.add_argument("--ws", required=True)
    p.add_argument("--admin-email", required=True)
    p.add_argument("--bidders", type=int, default=30)
    p.add_argument("--watchers", type=int, default=5)
    sys.exit(asyncio.run(main(p.parse_args())))
