"""DynamoDB data access. The concurrency-critical logic lives in `place_bid`.

How near-simultaneous bids are resolved
---------------------------------------
Each bid is ONE `TransactWriteItems` call with three items that commit atomically
or not at all:

  1. Update the auction, conditioned on
         status = OPEN  AND  startsAt <= now < endsAt  AND  minNextBid <= :amount
         AND  sellerId <> bidder  [AND termsVersion = the version the bidder saw]
     and set currentHigh/highBidder/minNextBid/version from the new bid.
  2. Put the bid record, conditioned on attribute_not_exists(bidId)
     (idempotency — a client retrying after a dropped connection can't double-bid).
  3. ConditionCheck the bidder's user row: buyerStatus = APPROVED
     (revoking approval takes effect atomically, never "between" check and write).

DynamoDB evaluates the conditions against the *committed* items at write time, so
the check and the write are a single atomic compare-and-set. There is no
read-modify-write window in application code and no "last write wins": a bid
that was valid when the user clicked but has since been beaten fails its
condition and is rejected with the current state.

When two transactions touch the same auction at the same instant, DynamoDB
cancels one with reason `TransactionConflict` instead of queueing it. We retry
those with jittered backoff; every retry re-evaluates the condition against the
newest committed state, so a retry can only succeed if the bid is *still* high
enough.

Listing edits and cancellation are conditional on `bidCount = 0`, so they can
never interleave with an accepted bid: whichever commits first wins.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from . import config
from .errors import Conflict, Forbidden, NotFound
from .models import (
    APPROVED, CANCELLED, OPEN, now_ms, parse_auction_update, phase, public_auction, public_bid,
)

_ser = TypeSerializer()
_deser = TypeDeserializer()

MAX_CONFLICT_RETRIES = 8


def _to_ddb(item: dict) -> dict:
    return {k: _ser.serialize(v) for k, v in item.items()}


def _from_ddb(item: dict) -> dict:
    return {k: _deser.deserialize(v) for k, v in item.items()}


def _auctions():
    return config.dynamodb_resource().Table(config.auctions_table_name())


def _bids():
    return config.dynamodb_resource().Table(config.bids_table_name())


def _query(table, limit: int, max_pages: int = 10, **kwargs) -> list[dict]:
    """Query that keeps paging until `limit` items survive any FilterExpression."""
    items: list[dict] = []
    for _ in range(max_pages):
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if len(items) >= limit or "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items[:limit]


# --------------------------------------------------------------------------- auctions

def create_auction(item: dict) -> dict:
    _auctions().put_item(Item=item, ConditionExpression="attribute_not_exists(auctionId)")
    return item


def get_auction(auction_id: str) -> dict | None:
    # ConsistentRead: a fresh page load / reconnect must see the latest committed
    # state, not a possibly-stale replica.
    return _auctions().get_item(Key={"auctionId": auction_id}, ConsistentRead=True).get("Item")


def get_auctions(auction_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    ids = list(dict.fromkeys(auction_ids))
    for i in range(0, len(ids), 100):
        request = {config.auctions_table_name(): {
            "Keys": [{"auctionId": a} for a in ids[i:i + 100]], "ConsistentRead": True}}
        while request:
            resp = config.dynamodb_resource().batch_get_item(RequestItems=request)
            for item in resp["Responses"].get(config.auctions_table_name(), []):
                out[item["auctionId"]] = item
            request = resp.get("UnprocessedKeys") or None
    return out


def list_auctions(category: str | None = None, limit: int = 50) -> list[dict]:
    """Newest first, cancelled listings hidden. Served from an index, not a Scan."""
    if category:
        index, key = "byCategory", Key("category").eq(category)
    else:
        index, key = "byListing", Key("listing").eq("ALL")
    return _query(_auctions(), limit, IndexName=index, KeyConditionExpression=key,
                  FilterExpression=Attr("status").ne(CANCELLED), ScanIndexForward=False)


def auctions_by_seller(seller_id: str, *, include_cancelled: bool, limit: int = 100) -> list[dict]:
    kwargs: dict = {"IndexName": "bySeller", "KeyConditionExpression": Key("sellerId").eq(seller_id),
                    "ScanIndexForward": False}
    if not include_cancelled:
        kwargs["FilterExpression"] = Attr("status").ne(CANCELLED)
    return _query(_auctions(), limit, **kwargs)


def recent_bids(auction_id: str, limit: int = 20) -> list[dict]:
    # Accepted bids strictly increase in amount, so ordering by the amount LSI
    # is also chronological order.
    resp = _bids().query(
        IndexName="byAmount",
        KeyConditionExpression=Key("auctionId").eq(auction_id),
        ScanIndexForward=False,
        Limit=limit,
        ConsistentRead=True,
    )
    return resp.get("Items", [])


def bids_by_bidder(bidder_id: str, limit: int = 200) -> list[dict]:
    # GSIs are eventually consistent: a bid placed a moment ago may take a second to appear.
    return _query(_bids(), limit, IndexName="byBidder",
                  KeyConditionExpression=Key("bidderId").eq(bidder_id), ScanIndexForward=False)


def get_bid(auction_id: str, bid_id: str) -> dict | None:
    resp = _bids().get_item(Key={"auctionId": auction_id, "bidId": bid_id}, ConsistentRead=True)
    return resp.get("Item")


def snapshot(auction_id: str) -> dict | None:
    auction = get_auction(auction_id)
    if auction is None:
        return None
    bids = recent_bids(auction_id)
    return {"auction": public_auction(auction), "bids": [public_bid(b) for b in bids]}


def close_auction(auction_id: str, at_ms: int | None = None) -> bool:
    """OPEN -> CLOSED, only once and only after endsAt. Returns True if this call closed it."""
    at = at_ms if at_ms is not None else now_ms()
    try:
        _auctions().update_item(
            Key={"auctionId": auction_id},
            UpdateExpression="SET #status = :closed, #version = #version + :one, updatedAt = :now",
            ConditionExpression="attribute_exists(auctionId) AND #status = :open AND endsAt <= :now",
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={":closed": "CLOSED", ":open": OPEN, ":one": 1, ":now": at},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _editable(current: dict | None, user_id: str, at: int, *, allow_admin: bool = False) -> dict:
    if current is None:
        raise NotFound("Auction not found.")
    if current.get("sellerId") != user_id and not allow_admin:
        raise Forbidden("Only the seller can change this auction.")
    p = phase(current, at)
    if p in ("CANCELLED", "ENDED"):
        raise Conflict(f"This auction has {'been cancelled' if p == 'CANCELLED' else 'ended'}.")
    if int(current.get("bidCount", 0)) > 0 and not allow_admin:
        raise Conflict("This auction already has bids, so it can no longer be changed.")
    return current


def update_auction(auction_id: str, user_id: str, body: dict, *, at_ms: int | None = None) -> tuple[dict, dict]:
    """Edit a listing before its first bid. Returns (new item, changed fields)."""
    at = at_ms if at_ms is not None else now_ms()
    current = _editable(get_auction(auction_id), user_id, at)
    changes = parse_auction_update(body, current, user_id, at)
    if "startingPrice" in changes:
        changes["minNextBid"] = changes["startingPrice"]  # no bids yet, so the floor is the start
    names = {f"#f_{k}": k for k in changes}
    values = {f":v_{k}": v for k, v in changes.items()}
    sets = ", ".join(f"#f_{k} = :v_{k}" for k in changes)
    try:
        new = _auctions().update_item(
            Key={"auctionId": auction_id},
            UpdateExpression=(f"SET {sets}, termsVersion = termsVersion + :one, "
                              "#version = #version + :one, updatedAt = :now"),
            # termsVersion is an optimistic lock between our read above and this write;
            # bidCount = 0 makes "edit" and "first bid" mutually exclusive.
            ConditionExpression=("sellerId = :me AND #status = :open AND bidCount = :zero "
                                 "AND termsVersion = :tv AND endsAt > :now"),
            ExpressionAttributeNames={**names, "#status": "status", "#version": "version"},
            ExpressionAttributeValues={**values, ":one": 1, ":now": at, ":me": user_id, ":open": OPEN,
                                       ":zero": 0, ":tv": int(current.get("termsVersion", 1))},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("The auction changed (or received a bid) while you were editing. Reload and try again.") from e
        raise
    return new, changes


def cancel_auction(auction_id: str, user_id: str, *, is_admin: bool = False, at_ms: int | None = None) -> dict:
    """Sellers may cancel before the first bid; admins may cancel any open auction."""
    at = at_ms if at_ms is not None else now_ms()
    _editable(get_auction(auction_id), user_id, at, allow_admin=is_admin)
    condition = "#status = :open AND endsAt > :now"
    values: dict = {":open": OPEN, ":now": at, ":cancelled": CANCELLED, ":one": 1}
    if not is_admin:
        condition += " AND sellerId = :me AND bidCount = :zero"
        values.update({":me": user_id, ":zero": 0})
    try:
        return _auctions().update_item(
            Key={"auctionId": auction_id},
            UpdateExpression="SET #status = :cancelled, #version = #version + :one, updatedAt = :now",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("The auction received a bid or ended, so it can no longer be cancelled.") from e
        raise


# --------------------------------------------------------------------------- bids

@dataclass
class BidOutcome:
    accepted: bool
    reason: str | None  # None when accepted; otherwise a machine-readable code
    message: str
    auction: dict | None = None  # public view of the auction after the attempt
    bid: dict | None = None
    duplicate: bool = False
    attempts: int = 1
    extra: dict = field(default_factory=dict)


def place_bid(bid: dict, *, at_ms: int | None = None, sleep=time.sleep) -> BidOutcome:
    """`bid` = auctionId, bidId, amount, bidderId, bidderName and optionally
    termsVersion. bidderId/bidderName must come from the authenticated user."""
    attempt = 0
    terms = bid.get("termsVersion")
    while True:
        attempt += 1
        at = at_ms if at_ms is not None else now_ms()
        bid_item = {
            "auctionId": bid["auctionId"],
            "bidId": bid["bidId"],
            "bidderId": bid["bidderId"],
            "bidderName": bid["bidderName"],
            "amount": bid["amount"],
            "placedAt": at,
        }
        condition = (
            "attribute_exists(auctionId) AND #status = :open "
            "AND endsAt > :now AND minNextBid <= :amount "
            "AND (attribute_not_exists(startsAt) OR startsAt <= :now) "
            "AND (attribute_not_exists(sellerId) OR sellerId <> :bidderId)"
        )
        values = {
            ":amount": bid["amount"],
            ":bidderId": bid["bidderId"],
            ":bidderName": bid["bidderName"],
            ":one": 1,
            ":open": OPEN,
            ":now": at,
        }
        if terms is not None:
            condition += " AND (attribute_not_exists(termsVersion) OR termsVersion = :tv)"
            values[":tv"] = terms
        try:
            config.dynamodb_client().transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": config.auctions_table_name(),
                            "Key": {"auctionId": {"S": bid["auctionId"]}},
                            "UpdateExpression": (
                                "SET currentHigh = :amount, "
                                "minNextBid = :amount + minIncrement, "
                                "highBidderId = :bidderId, "
                                "highBidderName = :bidderName, "
                                "bidCount = bidCount + :one, "
                                "#version = #version + :one, "
                                "updatedAt = :now"
                            ),
                            "ConditionExpression": condition,
                            "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                            "ExpressionAttributeValues": _to_ddb(values),
                            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
                        }
                    },
                    {
                        "Put": {
                            "TableName": config.bids_table_name(),
                            "Item": _to_ddb(bid_item),
                            "ConditionExpression": "attribute_not_exists(bidId)",
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": config.users_table_name(),
                            "Key": {"userId": {"S": bid["bidderId"]}},
                            "ConditionExpression": "buyerStatus = :approved",
                            "ExpressionAttributeValues": {":approved": {"S": APPROVED}},
                        }
                    },
                ],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = e.response.get("CancellationReasons") or [{}, {}, {}]
            codes = [r.get("Code", "None") for r in reasons]

            if "TransactionConflict" in codes and attempt <= MAX_CONFLICT_RETRIES:
                # Another bid on this auction committed at the same instant. Back off
                # and re-evaluate against the newly committed state.
                sleep(random.uniform(0, min(0.4, 0.015 * 2 ** attempt)))
                continue
            if "TransactionConflict" in codes:
                current = get_auction(bid["auctionId"])
                return BidOutcome(
                    False, "BUSY", "Too much contention on this auction, please retry.",
                    auction=public_auction(current, at) if current else None, attempts=attempt,
                )

            # Was this bid already accepted (a retry after a dropped connection)?
            # Check before blaming the amount, because a re-sent winning bid also
            # fails the auction condition now that it is itself the current high.
            existing = get_bid(bid["auctionId"], bid["bidId"])
            if existing is not None:
                return _resolve_duplicate(bid, attempt, existing)

            old = reasons[0].get("Item")
            current = _from_ddb(old) if old is not None else get_auction(bid["auctionId"])
            if len(codes) > 2 and codes[2] == "ConditionalCheckFailed":
                return BidOutcome(False, "NOT_APPROVED", "Your account is not approved for bidding yet.",
                                  auction=public_auction(current, at) if current else None, attempts=attempt)
            return _rejection(bid, current, at, attempt)

        # Committed.
        auction = get_auction(bid["auctionId"])
        return BidOutcome(
            True, None, "Bid accepted.",
            auction=public_auction(auction, at) if auction else None,
            bid=public_bid(bid_item), attempts=attempt,
        )


def _resolve_duplicate(bid: dict, attempt: int, existing: dict) -> BidOutcome:
    current = get_auction(bid["auctionId"])
    view = public_auction(current) if current else None
    if existing["bidderId"] != bid["bidderId"] or int(existing["amount"]) != bid["amount"]:
        return BidOutcome(False, "DUPLICATE_ID", "bidId was already used for a different bid.",
                          auction=view, attempts=attempt)
    return BidOutcome(True, None, "Bid already accepted (duplicate submission ignored).",
                      auction=view, bid=public_bid(existing), duplicate=True, attempts=attempt)


def _rejection(bid: dict, current: dict | None, at: int, attempt: int) -> BidOutcome:
    if current is None:
        return BidOutcome(False, "NOT_FOUND", "Auction does not exist.", attempts=attempt)
    view = public_auction(current, at)
    p = phase(current, at)

    def reject(reason: str, message: str, **extra) -> BidOutcome:
        return BidOutcome(False, reason, message, auction=view, attempts=attempt, extra=extra)

    if p == "CANCELLED":
        return reject("AUCTION_CLOSED", "This auction was cancelled.")
    if p == "ENDED":
        return reject("AUCTION_CLOSED", "This auction has ended.")
    if p == "SCHEDULED":
        return reject("NOT_STARTED", "This auction hasn't started yet.")
    if current.get("sellerId") == bid["bidderId"]:
        return reject("OWN_AUCTION", "You can't bid on your own auction.")
    terms = bid.get("termsVersion")
    if terms is not None and int(current.get("termsVersion", terms)) != terms:
        return reject("TERMS_CHANGED", "The seller changed this listing. Review it and bid again.")
    min_next = int(current["minNextBid"])
    if bid["amount"] < min_next:
        return reject(
            "BID_TOO_LOW",
            f"Bid must be at least {min_next} cents; current high is {view['currentHigh']}.",
            minNextBid=min_next,
        )
    # Condition failed but the state we read says it should pass: the auction moved
    # between the failed write and our read. Report it as stale rather than guess.
    return reject("STALE", "The auction changed while bidding; please retry.")
