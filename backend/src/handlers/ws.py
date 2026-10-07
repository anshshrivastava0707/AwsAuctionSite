"""API Gateway WebSocket API handler (all routes go to this one function).

Connect with `?token=<auth token>` to be able to bid (browsers can't set headers
on a WebSocket). Without a token the connection is a read-only viewer.

Client -> server messages (route selected by `action`):
  {"action": "subscribe", "auctionId": "..."}             -> snapshot (auction + recent bids)
  {"action": "watch",     "auctionIds": ["...", ...]}      -> watching (auctions only; up to 100)
  {"action": "placeBid",  "auctionId", "bidId", "amount", "termsVersion"?}
  {"action": "ping",      "auctionId"?: "..."}

The bidder is whoever authenticated the connection; a bidderId in the message is ignored.

Server -> client messages: see README "WebSocket protocol".
"""
from __future__ import annotations

import json
import logging

from auction import auth, connections, repository, users
from auction.models import ValidationError, parse_auction_id, parse_bid, parse_id, public_auction

log = logging.getLogger()
log.setLevel(logging.INFO)


def _endpoint(event: dict) -> str:
    ctx = event["requestContext"]
    return f"https://{ctx['domainName']}/{ctx['stage']}"


def _reply(event: dict, payload: dict) -> None:
    connections.send(_endpoint(event), event["requestContext"]["connectionId"], payload)


def handler(event, _context):
    route = event["requestContext"]["routeKey"]
    connection_id = event["requestContext"]["connectionId"]

    if route == "$connect":
        # A bad or expired token still connects, as an anonymous viewer: watching is
        # public, and the client finds out it must log in again when it tries to bid.
        token = (event.get("queryStringParameters") or {}).get("token")
        try:
            identity = auth.identify(token)
        except auth.AuthNotConfigured as e:
            log.error("auth not configured: %s", e)
            identity = None
        connections.register(connection_id, identity.user_id if identity else None)
        return {"statusCode": 200}

    if route == "$disconnect":
        # Only routing data is removed. Auction/bid state is never touched here,
        # and a bid that was in flight when the socket dropped either committed
        # atomically or didn't — the client resolves which by re-sending the same
        # bidId after reconnecting (idempotent).
        connections.remove(connection_id)
        return {"statusCode": 200}

    try:
        msg = json.loads(event.get("body") or "{}")
        if not isinstance(msg, dict):
            raise ValidationError("message must be a JSON object")
    except (json.JSONDecodeError, ValidationError):
        _reply(event, {"type": "error", "message": "Invalid JSON message."})
        return {"statusCode": 200}

    try:
        if route == "subscribe":
            _subscribe(event, connection_id, msg)
        elif route == "watch":
            _watch(event, connection_id, msg)
        elif route == "placeBid":
            _place_bid(event, connection_id, msg)
        elif route == "ping":
            _ping(event, msg)
        else:
            _reply(event, {"type": "error", "message": f"Unknown action: {msg.get('action')!r}"})
    except ValidationError as e:
        _reply(event, {"type": "error", "message": str(e), "requestAction": msg.get("action")})
    return {"statusCode": 200}


def _subscribe(event: dict, connection_id: str, msg: dict) -> None:
    auction_id = parse_auction_id(msg)
    # Order matters: register the subscription FIRST, then read the snapshot.
    # Any change committed after the read is broadcast to us (we're already
    # registered); any change before it is in the snapshot. Overlap is harmless
    # because clients drop updates whose version <= what they already have.
    connections.subscribe(connection_id, auction_id)
    snap = repository.snapshot(auction_id)
    if snap is None:
        _reply(event, {"type": "error", "message": "Auction not found.", "auctionId": auction_id})
        return
    _reply(event, {"type": "snapshot", **snap})


def _watch(event: dict, connection_id: str, msg: dict) -> None:
    """Follow several auctions on one connection (e.g. everything on "My bids").
    Same ordering rule as subscribe: register first, then read, so no update is
    lost in between. Each later auctionUpdate carries the full auction, so the
    client needs no bid history here."""
    ids = msg.get("auctionIds")
    if not isinstance(ids, list) or len(ids) > connections.MAX_SUBSCRIPTIONS:
        raise ValidationError(f"auctionIds must be a list of at most {connections.MAX_SUBSCRIPTIONS} ids")
    ids = [parse_id(i, "auctionIds") for i in ids]
    connections.subscribe_many(connection_id, ids)
    found = repository.get_auctions(ids)
    _reply(event, {"type": "watching", "auctions": [public_auction(found[i]) for i in ids if i in found]})


def _bid_reply(bid_id: str, outcome: repository.BidOutcome) -> dict:
    return {
        "type": "bidResult",
        "bidId": bid_id,
        "status": "ACCEPTED" if outcome.accepted else "REJECTED",
        "reason": outcome.reason,
        "message": outcome.message,
        "duplicate": outcome.duplicate,
        "auction": outcome.auction,
        "bid": outcome.bid,
        **outcome.extra,
    }


def _place_bid(event: dict, connection_id: str, msg: dict) -> None:
    bid = parse_bid(msg)
    conn = connections.get(connection_id) or {}
    user = users.get_user(conn["userId"]) if conn.get("userId") else None
    if user is None:
        reason, text = (("NOT_AUTHENTICATED", "Log in to bid.") if not conn.get("userId")
                        else ("NOT_APPROVED", "Finish setting up your account to bid."))
        _reply(event, _bid_reply(bid["bidId"], repository.BidOutcome(False, reason, text)))
        return
    # Identity and display name come from the account, never from the message.
    bid.update(bidderId=user["userId"], bidderName=user["displayName"])
    outcome = repository.place_bid(bid)
    log.info("bid %s on %s by %s amount=%s -> %s (%s attempts)", bid["bidId"], bid["auctionId"],
             bid["bidderId"], bid["amount"], outcome.reason or "ACCEPTED", outcome.attempts)
    # Only the bidder gets this direct reply. Everyone (including the bidder) gets
    # the state change via the DynamoDB Stream broadcast, which only fires for
    # committed writes.
    _reply(event, _bid_reply(bid["bidId"], outcome))


def _ping(event: dict, msg: dict) -> None:
    # Heartbeat keeps the connection inside API Gateway's 10-minute idle timeout.
    # It also returns the auction's current version as an anti-entropy check: a
    # client that sees a higher version than it holds knows it missed a broadcast
    # and re-subscribes for a fresh snapshot.
    reply: dict = {"type": "pong"}
    if msg.get("auctionId"):
        auction_id = parse_auction_id(msg)
        auction = repository.get_auction(auction_id)
        if auction is not None:
            reply.update(auctionId=auction_id, version=int(auction["version"]))
    _reply(event, reply)
