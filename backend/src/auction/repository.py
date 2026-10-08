"""DynamoDB data access. The concurrency-critical logic lives in `place_bid`.

How near-simultaneous bids are resolved
---------------------------------------
A bid is read -> plan -> one `TransactWriteItems` call that commits atomically or
not at all:

  1. Read the auction (strongly consistent).
  2. Plan the outcome in plain Python (`plan_bid`): the new price, leader and
     automatic-bidding ceiling, a soft-close extension, the bid records to write.
     Automatic bidding needs the leader's hidden ceiling, so a bid can no longer be
     decided by a condition expression alone.
  3. Commit, conditioned on
         version = the version we planned from  AND  status = OPEN
         AND  startsAt <= now < endsAt
     together with the bid records (attribute_not_exists(bidId): idempotency — a
     client retrying after a dropped connection can't double-bid) and a
     ConditionCheck that the bidder's buyerStatus = APPROVED (revoking approval
     takes effect atomically, never "between" check and write).

`version` is bumped by every bid, edit and close, so the version condition makes
the read and the write one atomic compare-and-set: if anything changed in
between, the write fails, and we re-plan from the state DynamoDB hands back with
the failure (ReturnValuesOnConditionCheckFailure) — no extra read. A bid that was
valid when the user clicked but has since been beaten is re-planned against the
newer state and rejected. There is no "last write wins".

When two transactions touch the same auction at the same instant, DynamoDB
cancels one with `TransactionConflict` instead of queueing it. We retry those with
jittered backoff, re-reading first.

Listing edits and cancellation are conditional on `bidCount = 0`, so they can
never interleave with an accepted bid: whichever commits first wins.

Saving an auction ("watching") changes `watchCount` without bumping `version`, so
saves never force a bid to re-plan.
"""
from __future__ import annotations

import base64
import json
import random
import time
from dataclasses import dataclass, field

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from . import config
from .errors import Conflict, Forbidden, NotFound
from .models import (
    APPROVED, CANCELLED, CLOSED, EXTEND_WINDOW_MS, OPEN, OPEN_LISTING, now_ms, parse_auction_update, phase,
    public_auction, public_bid, search_text,
)

_ser = TypeSerializer()
_deser = TypeDeserializer()

MAX_CONFLICT_RETRIES = 8  # TransactionConflict: another write in flight
MAX_REPLANS = 60  # version moved on: re-plan against the newer state


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


SORTS = ("newest", "ending", "price_low", "price_high")
PAGE_SIZE = 24
MAX_SCAN_RESULTS = 1000  # search and price sorts work on at most this many matches


def _encode_cursor(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, default=int).encode()).decode()


def _decode_cursor(cursor: str | None) -> dict | None:
    if not cursor:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def _keyed_page(index: str, key, *, limit: int, cursor: dict | None, key_attrs: tuple[str, ...],
                forward: bool, filter_expr) -> tuple[list[dict], str | None]:
    """One page from an index. The cursor is rebuilt from the last item we return
    (not DynamoDB's LastEvaluatedKey), because the filter means we may stop
    part-way through a DynamoDB page."""
    kwargs: dict = {"IndexName": index, "KeyConditionExpression": key, "ScanIndexForward": forward,
                    "FilterExpression": filter_expr}
    if cursor:
        kwargs["ExclusiveStartKey"] = cursor
    items: list[dict] = []
    more = False
    for _ in range(20):
        resp = _auctions().query(**kwargs)
        items.extend(resp.get("Items", []))
        more = "LastEvaluatedKey" in resp
        if len(items) > limit or not more:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    page = items[:limit]
    if (len(items) > limit or more) and page:
        last = page[-1]
        return page, _encode_cursor({a: last[a] for a in key_attrs})
    return page, None


def _price(a: dict) -> int:
    return int(a["currentHigh"]) if a.get("currentHigh") is not None else int(a["startingPrice"])


def list_auctions(category: str | None = None, limit: int = 50, *, sort: str = "newest",
                  query: str = "", cursor: str | None = None, at_ms: int | None = None) -> tuple[list[dict], str | None]:
    """A page of auctions and the cursor for the next one (None at the end).

    newest   all non-cancelled listings, newest first (byListing / byCategory index)
    ending   open listings by end time, soonest first (byEnding index)
    price_*  open listings by current price (sorted here, up to MAX_SCAN_RESULTS)
    query    words that must all appear in the listing; a filtered Scan, fine for
             thousands of listings. Past that, move search to OpenSearch.
    """
    at = at_ms if at_ms is not None else now_ms()
    words = [w for w in query.lower().split() if w][:8]
    if sort not in SORTS:
        sort = "newest"
    not_cancelled = Attr("status").ne(CANCELLED)

    if not words and sort == "newest":
        if category:
            index, key, attrs = "byCategory", Key("category").eq(category), ("auctionId", "category", "createdAt")
        else:
            index, key, attrs = "byListing", Key("listing").eq("ALL"), ("auctionId", "listing", "createdAt")
        return _keyed_page(index, key, limit=limit, cursor=_decode_cursor(cursor), key_attrs=attrs,
                           forward=False, filter_expr=not_cancelled)

    if not words and sort == "ending":
        f = Attr("endsAt").gt(at)
        if category:
            f = f & Attr("category").eq(category)
        return _keyed_page("byEnding", Key("openListing").eq(OPEN_LISTING), limit=limit,
                           cursor=_decode_cursor(cursor), key_attrs=("auctionId", "openListing", "endsAt"),
                           forward=True, filter_expr=f)

    # Sorted in memory: price sorts over open listings, or any sort over search matches.
    if words:
        f = not_cancelled
        for w in words:
            f = f & Attr("searchText").contains(w)
        if category:
            f = f & Attr("category").eq(category)
        if sort != "newest":
            f = f & Attr("openListing").exists()
        items = _scan(f)
    else:
        f = Attr("endsAt").gt(at)
        if category:
            f = f & Attr("category").eq(category)
        items = _query(_auctions(), MAX_SCAN_RESULTS, max_pages=50, IndexName="byEnding",
                       KeyConditionExpression=Key("openListing").eq(OPEN_LISTING), FilterExpression=f)
    if sort == "newest":
        items.sort(key=lambda a: -int(a["createdAt"]))
    elif sort == "ending":
        items.sort(key=lambda a: int(a["endsAt"]))
    else:
        items.sort(key=_price, reverse=sort == "price_high")
    offset = int((_decode_cursor(cursor) or {}).get("offset", 0))
    page = items[offset:offset + limit]
    more = offset + limit < len(items)
    return page, _encode_cursor({"offset": offset + limit}) if more else None


def _scan(filter_expr) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {"FilterExpression": filter_expr}
    while len(items) < MAX_SCAN_RESULTS:
        resp = _auctions().scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items[:MAX_SCAN_RESULTS]


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
            UpdateExpression="SET #status = :closed, #version = #version + :one, updatedAt = :now REMOVE openListing",
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
        changes["price"] = changes["startingPrice"]
    if {"title", "description", "category"} & set(changes):
        changes["searchText"] = search_text({**current, **changes}, current.get("sellerName") or "")
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
            UpdateExpression="SET #status = :cancelled, #version = #version + :one, updatedAt = :now REMOVE openListing",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("The auction received a bid or ended, so it can no longer be cancelled.") from e
        raise


def remove_auction(auction_id: str, admin_id: str, reason: str, *, at_ms: int | None = None) -> dict:
    """Admin takedown (e.g. a listing that breaks the terms). Works in any state
    except already cancelled — live, upcoming or ended — and hides the listing
    everywhere, the same way a cancellation does. Bumping `version` means a bid
    racing with the takedown fails its condition, re-plans and is rejected."""
    at = at_ms if at_ms is not None else now_ms()
    try:
        return _auctions().update_item(
            Key={"auctionId": auction_id},
            UpdateExpression=("SET #status = :cancelled, removedAt = :now, removedBy = :admin, removedReason = :reason, "
                              "#version = #version + :one, updatedAt = :now REMOVE openListing"),
            ConditionExpression="attribute_exists(auctionId) AND #status <> :cancelled",
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={":cancelled": CANCELLED, ":now": at, ":admin": admin_id, ":reason": reason,
                                       ":one": 1},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        if get_auction(auction_id) is None:
            raise NotFound("Auction not found.") from e
        raise Conflict("This auction has already been removed or cancelled.") from e


# --------------------------------------------------------------------------- saves ("watching")

def _saves():
    return config.dynamodb_resource().Table(config.saves_table_name())


def set_saved(user_id: str, auction_id: str, saved: bool) -> bool:
    """Save or unsave, keeping the auction's watchCount in step in the same
    transaction. Idempotent: returns False when nothing changed. Doesn't bump the
    auction's version, so it never makes an in-flight bid re-plan."""
    key = {"userId": {"S": user_id}, "auctionId": {"S": auction_id}}
    if saved:
        row = {"Put": {"TableName": config.saves_table_name(),
                       "Item": {**key, "savedAt": {"N": str(now_ms())}},
                       "ConditionExpression": "attribute_not_exists(userId)"}}
    else:
        row = {"Delete": {"TableName": config.saves_table_name(), "Key": key,
                          "ConditionExpression": "attribute_exists(userId)"}}
    try:
        config.dynamodb_client().transact_write_items(TransactItems=[row, {
            "Update": {
                "TableName": config.auctions_table_name(),
                "Key": {"auctionId": {"S": auction_id}},
                "UpdateExpression": "ADD watchCount :d",
                "ConditionExpression": "attribute_exists(auctionId)",
                "ExpressionAttributeValues": {":d": {"N": "1" if saved else "-1"}},
            }
        }])
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        codes = [r.get("Code", "None") for r in e.response.get("CancellationReasons") or []]
        if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
            raise NotFound("Auction not found.") from e
        if codes and codes[0] == "ConditionalCheckFailed":
            return False  # already saved / not saved
        raise Conflict("Busy, please retry.") from e


def saved_ids(user_id: str, limit: int = 500) -> list[str]:
    """Most recently saved first."""
    items = _query(_saves(), limit, KeyConditionExpression=Key("userId").eq(user_id))
    items.sort(key=lambda i: -int(i.get("savedAt", 0)))
    return [i["auctionId"] for i in items]


def savers(auction_id: str) -> list[str]:
    items = _query(_saves(), 10_000, max_pages=100, IndexName="byAuction",
                   KeyConditionExpression=Key("auctionId").eq(auction_id))
    return [i["userId"] for i in items]


def bidders(auction_id: str) -> list[str]:
    """Everyone who has bid on an auction (for "ending soon" reminders)."""
    items = _query(_bids(), 10_000, max_pages=100, KeyConditionExpression=Key("auctionId").eq(auction_id),
                   ProjectionExpression="bidderId")
    return list(dict.fromkeys(i["bidderId"] for i in items))


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


@dataclass
class BidPlan:
    """What one bid does to an auction, decided before anything is written."""
    sets: dict  # auction attributes to SET
    removes: list[str]
    records: list[dict]  # bid rows to put; rows without `amount` are private (a raised maximum)
    leading: bool  # does the bidder lead once this commits?
    message: str
    extra: dict = field(default_factory=dict)


class BidRejected(Exception):
    def __init__(self, reason: str, message: str, **extra):
        super().__init__(message)
        self.reason, self.message, self.extra = reason, message, extra


def _num(value) -> int | None:
    return None if value is None else int(value)


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}".replace(".00", "")


def plan_bid(state: dict, bid: dict, at: int) -> BidPlan:
    """Pure function of the current auction row and the bid; raises BidRejected.

    Automatic bidding works like eBay's: every bidder has a ceiling (their
    `maxAmount`, or just `amount` for a plain bid). The leader's ceiling is kept
    on the auction row as `proxyMax` and never shown to anyone. A challenger
    whose ceiling is higher takes the lead at one increment over the old ceiling
    (or at their own `amount` if that is higher); otherwise the leader's
    automatic bid answers at one increment over the challenger (ties go to the
    earlier bidder). With a reserve, a ceiling at or above the reserve lifts the
    price straight to the reserve."""
    p = phase(state, at)
    if p == "CANCELLED":
        raise BidRejected("AUCTION_CLOSED", "This auction was cancelled.")
    if p == "ENDED":
        raise BidRejected("AUCTION_CLOSED", "This auction has ended.")
    if p == "SCHEDULED":
        raise BidRejected("NOT_STARTED", "This auction hasn't started yet.")
    me, my_name = bid["bidderId"], bid["bidderName"]
    if state.get("sellerId") == me:
        raise BidRejected("OWN_AUCTION", "You can't bid on your own auction.")
    terms = bid.get("termsVersion")
    if terms is not None and int(state.get("termsVersion", terms)) != terms:
        raise BidRejected("TERMS_CHANGED", "The seller changed this listing. Review it and bid again.")

    cur = _num(state.get("currentHigh"))
    leader, leader_name = state.get("highBidderId"), state.get("highBidderName")
    ceiling = _num(state.get("proxyMax"))
    if ceiling is None:
        ceiling = cur
    min_next, inc = int(state["minNextBid"]), int(state["minIncrement"])
    reserve = _num(state.get("reservePrice"))
    amount = bid["amount"]
    my_max = max(amount, bid.get("maxAmount") or amount)
    auto_id = f"auto-{bid['bidId']}"

    def row(bid_id: str, bidder: str, name: str, amt: int | None, order: int, **extra) -> dict:
        r = {"auctionId": state["auctionId"], "bidId": bid_id, "bidderId": bidder, "bidderName": name,
             "placedAt": at + order, **extra}
        if amt is not None:
            r["amount"] = amt
        return r

    def mine(amt: int | None, order: int) -> dict:
        extra = {"requestedAmount": amount}
        if bid.get("maxAmount"):
            extra["maxAmount"] = my_max
        return row(bid["bidId"], me, my_name, amt, order, **extra)

    def lift_to_reserve(price: int, max_of_leader: int) -> int:
        return reserve if reserve is not None and price < reserve <= max_of_leader else price

    def leader_sets(price: int, **more) -> dict:
        return {"currentHigh": price, "minNextBid": price + inc, "price": price, **more}

    if bid.get("buyNow"):
        price = _num(state.get("buyNowPrice"))
        if price is None or int(state.get("bidCount") or 0) > 0:
            raise BidRejected("BUY_NOW_UNAVAILABLE", "Buy it now is no longer available on this auction.")
        if amount != price:
            raise BidRejected("TERMS_CHANGED", "The Buy it now price changed. Review it and try again.")
        return BidPlan(
            sets=leader_sets(price, highBidderId=me, highBidderName=my_name, proxyMax=price,
                             status=CLOSED, endsAt=at, soldVia="BUY_NOW"),
            removes=["openListing"], records=[mine(price, 0) | {"buyNow": True}], leading=True,
            message=f"You bought it for {_dollars(price)}!",
        )

    if leader == me:
        # Raising your own bid: an explicit higher bid, a higher ceiling, or both.
        if bid.get("maxAmount"):
            new_max, price = max(ceiling, my_max), cur
        elif amount < min_next:
            raise BidRejected("BID_TOO_LOW",
                              f"Bid must be at least {min_next} cents; current high is {cur}.", minNextBid=min_next)
        else:
            new_max, price = max(ceiling, amount), amount
        price = lift_to_reserve(price, new_max)
        if new_max == ceiling and price == cur:
            raise BidRejected("ALREADY_LEADING", "You're already the highest bidder. To go higher, raise your maximum bid.")
        if price != cur:
            plan = BidPlan(sets=leader_sets(price, proxyMax=new_max), removes=[], records=[mine(price, 0)],
                           leading=True, message="Bid accepted.")
        else:
            plan = BidPlan(sets={"proxyMax": new_max}, removes=[], records=[mine(None, 0)], leading=True,
                           message=f"Your maximum bid is now {_dollars(new_max)}.")
    elif amount < min_next:
        raise BidRejected("BID_TOO_LOW",
                          f"Bid must be at least {min_next} cents; current high is {cur}.", minNextBid=min_next)
    elif leader is None or my_max > ceiling:
        # The challenger takes the lead.
        price = amount if leader is None else max(amount, min(my_max, ceiling + inc))
        price = lift_to_reserve(price, my_max)
        records = []
        if leader is not None and ceiling > cur:
            # The old leader's automatic bids went all the way to their ceiling.
            records.append(row(auto_id, leader, leader_name, ceiling, 0, auto=True))
        records.append(mine(price, 1))
        plan = BidPlan(sets=leader_sets(price, highBidderId=me, highBidderName=my_name, proxyMax=my_max),
                       removes=[], records=records, leading=True, message="Bid accepted.")
    else:
        # The leader's automatic bid answers; the challenger is recorded at their own ceiling.
        answer = lift_to_reserve(min(ceiling, my_max + inc), ceiling)
        plan = BidPlan(
            sets=leader_sets(answer),
            removes=[], records=[mine(my_max, 0), row(auto_id, leader, leader_name, answer, 1, auto=True)],
            leading=False,
            message="Another bidder's automatic bid is higher, so you've been outbid. Try a higher maximum.",
        )

    # Soft close: a bid in the final minutes pushes the end out, so nobody wins by
    # bidding in the last second. endsAt only ever moves later (endsAt < at + window).
    if int(state["endsAt"]) - at < EXTEND_WINDOW_MS:
        plan.sets["endsAt"] = at + EXTEND_WINDOW_MS
        plan.extra["extendedTo"] = at + EXTEND_WINDOW_MS
    if plan.leading:
        plan.extra["yourMax"] = plan.sets.get("proxyMax", ceiling)
    return plan


def _commit(state: dict, bid: dict, plan: BidPlan, at: int) -> None:
    visible = [r for r in plan.records if "amount" in r]
    sets = {**plan.sets, "updatedAt": at, "lastBids": [public_bid(r) for r in visible]}
    names = {f"#f_{k}": k for k in sets} | {f"#r_{k}": k for k in plan.removes}
    values = {f":v_{k}": v for k, v in sets.items()}
    update = ("SET " + ", ".join(f"#f_{k} = :v_{k}" for k in sets)
              + ", #version = #version + :one, bidCount = bidCount + :n")
    if plan.removes:
        update += " REMOVE " + ", ".join(f"#r_{k}" for k in plan.removes)
    values.update({":one": 1, ":n": len(visible), ":v": int(state["version"]), ":open": OPEN, ":now": at})
    items = [{
        "Update": {
            "TableName": config.auctions_table_name(),
            "Key": {"auctionId": {"S": state["auctionId"]}},
            "UpdateExpression": update,
            "ConditionExpression": (
                "attribute_exists(auctionId) AND #version = :v AND #status = :open AND endsAt > :now "
                "AND (attribute_not_exists(startsAt) OR startsAt <= :now)"
            ),
            "ExpressionAttributeNames": {**names, "#status": "status", "#version": "version"},
            "ExpressionAttributeValues": _to_ddb(values),
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }
    }]
    items += [{"Put": {"TableName": config.bids_table_name(), "Item": _to_ddb(r),
                       "ConditionExpression": "attribute_not_exists(bidId)"}} for r in plan.records]
    items.append({
        "ConditionCheck": {
            "TableName": config.users_table_name(),
            "Key": {"userId": {"S": bid["bidderId"]}},
            "ConditionExpression": "buyerStatus = :approved",
            "ExpressionAttributeValues": {":approved": {"S": APPROVED}},
        }
    })
    config.dynamodb_client().transact_write_items(TransactItems=items)


def place_bid(bid: dict, *, at_ms: int | None = None, sleep=time.sleep) -> BidOutcome:
    """`bid` = auctionId, bidId, amount, bidderId, bidderName and optionally
    termsVersion, maxAmount, buyNow. bidderId/bidderName must come from the
    authenticated user."""
    conflicts = replans = 0
    state = get_auction(bid["auctionId"])
    while True:
        at = at_ms if at_ms is not None else now_ms()
        attempts = 1 + conflicts + replans
        if state is None:
            return BidOutcome(False, "NOT_FOUND", "Auction does not exist.", attempts=attempts)
        try:
            plan = plan_bid(state, bid, at)
        except BidRejected as r:
            # Was this bid already accepted (a retry after a dropped connection)?
            # Check before blaming the amount: a re-sent winning bid no longer
            # beats the auction it already leads.
            existing = get_bid(bid["auctionId"], bid["bidId"])
            if existing is not None:
                return _resolve_duplicate(bid, attempts, existing)
            return BidOutcome(False, r.reason, r.message, auction=public_auction(state, at),
                              attempts=attempts, extra=r.extra)
        try:
            _commit(state, bid, plan, at)
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = e.response.get("CancellationReasons") or []
            codes = [r.get("Code", "None") for r in reasons]

            if "TransactionConflict" in codes:
                # Another write on this auction is in flight. Back off, then re-read.
                conflicts += 1
                if conflicts > MAX_CONFLICT_RETRIES:
                    return BidOutcome(False, "BUSY", "Too much contention on this auction, please retry.",
                                      auction=public_auction(state, at), attempts=attempts)
                sleep(random.uniform(0, min(0.4, 0.015 * 2 ** conflicts)))
                state = get_auction(bid["auctionId"])
                continue
            if "ConditionalCheckFailed" in codes[1:-1]:
                existing = get_bid(bid["auctionId"], bid["bidId"])
                if existing is not None:
                    return _resolve_duplicate(bid, attempts, existing)
            if codes and codes[-1] == "ConditionalCheckFailed":
                return BidOutcome(False, "NOT_APPROVED", "Your account is not approved for bidding yet.",
                                  auction=public_auction(state, at), attempts=attempts)
            # The auction moved on since we planned (or its time is up): re-plan
            # against the state DynamoDB returned with the failure.
            replans += 1
            if replans > MAX_REPLANS:
                return BidOutcome(False, "BUSY", "Too much contention on this auction, please retry.",
                                  auction=public_auction(state, at), attempts=attempts)
            old = reasons[0].get("Item") if reasons else None
            state = _from_ddb(old) if old else get_auction(bid["auctionId"])
            continue

        # Committed.
        auction = get_auction(bid["auctionId"])
        mine = next((r for r in plan.records if r["bidId"] == bid["bidId"]), None)
        return BidOutcome(
            True, None, plan.message,
            auction=public_auction(auction, at) if auction else None,
            bid=public_bid(mine) if mine and "amount" in mine else None, attempts=attempts,
            extra={"leading": plan.leading, **plan.extra},
        )


def _resolve_duplicate(bid: dict, attempt: int, existing: dict) -> BidOutcome:
    current = get_auction(bid["auctionId"])
    view = public_auction(current) if current else None
    requested = int(existing.get("requestedAmount", existing.get("amount", -1)))
    if existing["bidderId"] != bid["bidderId"] or requested != bid["amount"]:
        return BidOutcome(False, "DUPLICATE_ID", "bidId was already used for a different bid.",
                          auction=view, attempts=attempt)
    return BidOutcome(True, None, "Bid already accepted (duplicate submission ignored).",
                      auction=view, bid=public_bid(existing) if "amount" in existing else None,
                      duplicate=True, attempts=attempt)
