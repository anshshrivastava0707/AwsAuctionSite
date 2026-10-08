"""Input validation and the public (client-facing) shape of auctions, bids and users.

All money is integer cents end to end — never floats. All times are epoch milliseconds (UTC).

The Lambda runtime only ships boto3, so validation is hand-rolled instead of
pulling in pydantic (keeps packages tiny and cold starts fast, and avoids
compiling native wheels for the Lambda architecture).

The TypeScript mirror of these shapes lives in frontend/lib/types.ts.
"""
from __future__ import annotations

import re
import time
import uuid
from decimal import Decimal
from typing import Any

MAX_AMOUNT_CENTS = 100_000_000_00  # $100M sanity cap
MAX_DURATION_SECONDS = 7 * 24 * 3600
MIN_DURATION_SECONDS = 30
MAX_START_DELAY_SECONDS = 30 * 24 * 3600
CLOCK_SKEW_MS = 60_000  # a start time this far in the past still counts as "now"
MAX_IMAGES = 8
MAX_QUANTITY = 1000
# Soft close: a bid this close to the end pushes the end out to now + this.
EXTEND_WINDOW_MS = 2 * 60 * 1000
# Constant partition key of the byEnding index. Present on every listing that is
# still on the site (removed only by a cancellation or an admin takedown); closed
# auctions keep it, and queries pick live / recently ended ones by endsAt range.
OPEN_LISTING = "OPEN"
# How long an ended auction stays on the browse pages before it drops off.
RECENTLY_ENDED_MS = 15 * 60 * 1000

# Keep in sync with frontend/lib/types.ts.
CATEGORIES = (
    "ELECTRONICS", "COLLECTIBLES", "FASHION", "HOME_GARDEN", "ART",
    "SPORTS", "TOYS", "VEHICLES", "BOOKS", "OTHER",
)
CONDITIONS = ("NEW", "LIKE_NEW", "GOOD", "FAIR", "FOR_PARTS")

# Stored status. SCHEDULED/LIVE/ENDED are *derived* from the clock (see `phase`),
# so no background job is needed for an auction to start.
OPEN, CLOSED, CANCELLED = "OPEN", "CLOSED", "CANCELLED"

# Per-role approval state on a user account.
NONE, PENDING, APPROVED, REJECTED = "NONE", "PENDING", "APPROVED", "REJECTED"
APPROVAL_STATES = (NONE, PENDING, APPROVED, REJECTED)

IMAGE_KEY_RE = re.compile(r"^uploads/[A-Za-z0-9_-]{1,64}/[0-9a-f]{32}\.(jpg|png|webp|gif)$")


class ValidationError(ValueError):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def _int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    # bool is an int subclass; reject it explicitly. Reject floats so 10.5 cents can't sneak in.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValidationError(f"{field} must be between {minimum} and {maximum}")
    return value


def _str(value: Any, field: str, *, max_len: int, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"{field} is required")
    if len(value) > max_len:
        raise ValidationError(f"{field} must be at most {max_len} characters")
    return value


def _id(value: Any, field: str) -> str:
    value = _str(value, field, max_len=64)
    if not all(c.isalnum() or c in "-_" for c in value):
        raise ValidationError(f"{field} contains invalid characters")
    return value


def _choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ValidationError(f"{field} must be one of {', '.join(choices)}")
    return value


def image_key(value: Any, field: str, owner_id: str) -> str:
    """An uploaded image may only be attached by the user who uploaded it."""
    if not isinstance(value, str) or not IMAGE_KEY_RE.match(value):
        raise ValidationError(f"{field} is not a valid uploaded image")
    if not value.startswith(f"uploads/{owner_id}/"):
        raise ValidationError(f"{field} was not uploaded by you")
    return value


def _images(value: Any, owner_id: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("images must be a list")
    if len(value) > MAX_IMAGES:
        raise ValidationError(f"at most {MAX_IMAGES} images are allowed")
    keys = [image_key(v, "images", owner_id) for v in value]
    if len(set(keys)) != len(keys):
        raise ValidationError("images contains duplicates")
    return keys


# --------------------------------------------------------------------------- auctions

_LISTING_FIELDS = ("title", "description", "category", "condition", "quantity", "images",
                   "startingPrice", "minIncrement", "reservePrice", "buyNowPrice", "startsAt", "endsAt")


def _validate_listing_field(name: str, value: Any, owner_id: str) -> Any:
    if name == "title":
        return _str(value, "title", max_len=120)
    if name == "description":
        return _str(value, "description", max_len=2000, required=False)
    if name == "category":
        return _choice(value, "category", CATEGORIES)
    if name == "condition":
        return _choice(value, "condition", CONDITIONS)
    if name == "quantity":
        return _int(value, "quantity", minimum=1, maximum=MAX_QUANTITY)
    if name == "images":
        return _images(value, owner_id)
    if name == "startingPrice":
        return _int(value, "startingPrice", minimum=0, maximum=MAX_AMOUNT_CENTS)
    if name == "minIncrement":
        return _int(value, "minIncrement", minimum=1, maximum=MAX_AMOUNT_CENTS)
    if name in ("reservePrice", "buyNowPrice"):
        return None if value is None else _int(value, name, minimum=1, maximum=MAX_AMOUNT_CENTS)
    if name in ("startsAt", "endsAt"):
        return _int(value, name, minimum=0, maximum=2**53)
    raise ValidationError(f"unknown field {name}")


def _validate_schedule(starts_at: int, ends_at: int, at: int, *, check_start: bool) -> None:
    if check_start:
        if starts_at < at - CLOCK_SKEW_MS:
            raise ValidationError("startsAt must not be in the past")
        if starts_at > at + MAX_START_DELAY_SECONDS * 1000:
            raise ValidationError("startsAt must be within 30 days")
    duration = ends_at - max(starts_at, at)
    if duration < MIN_DURATION_SECONDS * 1000:
        raise ValidationError(f"the auction must run for at least {MIN_DURATION_SECONDS} seconds")
    if ends_at - starts_at > MAX_DURATION_SECONDS * 1000:
        raise ValidationError("the auction can run for at most 7 days")


def _validate_prices(starting: int, reserve: int | None, buy_now: int | None) -> None:
    if reserve is not None and reserve <= starting:
        raise ValidationError("the reserve price must be above the starting price")
    if buy_now is not None:
        if buy_now <= starting:
            raise ValidationError("the Buy it now price must be above the starting price")
        if reserve is not None and buy_now < reserve:
            raise ValidationError("the Buy it now price must be at least the reserve price")


def parse_create_auction(body: Any, owner_id: str, at_ms: int | None = None) -> dict:
    """Times are either absolute (`startsAt`/`endsAt`) or, for scripts, a
    `durationSeconds` that starts the auction immediately."""
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    at = at_ms if at_ms is not None else now_ms()
    data = {
        "title": _validate_listing_field("title", body.get("title"), owner_id),
        "description": _validate_listing_field("description", body.get("description"), owner_id),
        "category": _validate_listing_field("category", body.get("category", "OTHER"), owner_id),
        "condition": _validate_listing_field("condition", body.get("condition", "GOOD"), owner_id),
        "quantity": _validate_listing_field("quantity", body.get("quantity", 1), owner_id),
        "images": _validate_listing_field("images", body.get("images"), owner_id),
        "startingPrice": _validate_listing_field("startingPrice", body.get("startingPrice"), owner_id),
        "minIncrement": _validate_listing_field("minIncrement", body.get("minIncrement", 100), owner_id),
        "reservePrice": _validate_listing_field("reservePrice", body.get("reservePrice"), owner_id),
        "buyNowPrice": _validate_listing_field("buyNowPrice", body.get("buyNowPrice"), owner_id),
    }
    _validate_prices(data["startingPrice"], data["reservePrice"], data["buyNowPrice"])
    if "endsAt" in body:
        starts_at = _validate_listing_field("startsAt", body.get("startsAt", at), owner_id)
        ends_at = _validate_listing_field("endsAt", body["endsAt"], owner_id)
    else:
        duration = _int(body.get("durationSeconds"), "durationSeconds",
                        minimum=MIN_DURATION_SECONDS, maximum=MAX_DURATION_SECONDS)
        starts_at, ends_at = at, at + duration * 1000
    _validate_schedule(starts_at, ends_at, at, check_start=True)
    data["startsAt"] = max(starts_at, at)  # a start "a few seconds ago" means now
    data["endsAt"] = ends_at
    return data


def parse_auction_update(body: Any, current: dict, owner_id: str, at_ms: int | None = None) -> dict:
    """Partial update of a listing. Returns only the fields that change."""
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    unknown = set(body) - set(_LISTING_FIELDS)
    if unknown:
        raise ValidationError(f"cannot change: {', '.join(sorted(unknown))}")
    at = at_ms if at_ms is not None else now_ms()
    changes = {k: _validate_listing_field(k, v, owner_id) for k, v in body.items()}
    current = {k: _plain(v) for k, v in current.items()}
    changes = {k: v for k, v in changes.items() if current.get(k) != v}
    if not changes:
        raise ValidationError("nothing to change")
    if {"startingPrice", "reservePrice", "buyNowPrice"} & set(changes):
        merged = {**current, **changes}
        _validate_prices(merged["startingPrice"], merged.get("reservePrice"), merged.get("buyNowPrice"))
    if "startsAt" in changes or "endsAt" in changes:
        starts_at = changes.get("startsAt", current.get("startsAt", current["createdAt"]))
        ends_at = changes.get("endsAt", current["endsAt"])
        _validate_schedule(starts_at, ends_at, at, check_start="startsAt" in changes)
    return changes


def parse_bid(msg: dict) -> dict:
    """The bidder's identity is NOT taken from the message — it comes from the
    authenticated connection (see handlers/ws.py).

    `amount` is the bid placed now. `maxAmount` (optional, >= amount) turns on
    automatic bidding: the system bids for this bidder, one increment at a time,
    up to that ceiling. `buyNow: true` buys at the listing's Buy it now price,
    which must equal `amount` (so the price can't change under the buyer)."""
    bid = {
        "auctionId": _id(msg.get("auctionId"), "auctionId"),
        # Leaves room for the "auto-" prefix of the automatic bids it triggers.
        "bidId": _str(_id(msg.get("bidId"), "bidId"), "bidId", max_len=58),
        "amount": _int(msg.get("amount"), "amount", minimum=1, maximum=MAX_AMOUNT_CENTS),
    }
    if msg.get("termsVersion") is not None:
        bid["termsVersion"] = _int(msg["termsVersion"], "termsVersion", minimum=1, maximum=2**31)
    if msg.get("maxAmount") is not None:
        bid["maxAmount"] = _int(msg["maxAmount"], "maxAmount", minimum=bid["amount"], maximum=MAX_AMOUNT_CENTS)
    if msg.get("buyNow") is not None:
        if not isinstance(msg["buyNow"], bool):
            raise ValidationError("buyNow must be true or false")
        if msg["buyNow"]:
            if "maxAmount" in bid:
                raise ValidationError("buyNow can't be combined with maxAmount")
            bid["buyNow"] = True
    return bid


def parse_auction_id(msg: dict) -> str:
    return _id(msg.get("auctionId"), "auctionId")


def parse_id(value: Any, field: str) -> str:
    return _id(value, field)


def new_auction_item(data: dict, seller: dict, created_at_ms: int | None = None) -> dict:
    created = created_at_ms if created_at_ms is not None else now_ms()
    return {
        "auctionId": uuid.uuid4().hex,
        "sellerId": seller["userId"],
        "sellerName": seller["displayName"],
        "title": data["title"],
        "description": data["description"],
        "category": data["category"],
        "condition": data["condition"],
        "quantity": data["quantity"],
        "images": data["images"],
        "startingPrice": data["startingPrice"],
        "minIncrement": data["minIncrement"],
        "reservePrice": data.get("reservePrice"),
        "buyNowPrice": data.get("buyNowPrice"),
        # minNextBid is denormalised because DynamoDB condition expressions
        # cannot do arithmetic: the bid condition is simply `minNextBid <= :amount`.
        "minNextBid": data["startingPrice"],
        "currentHigh": None,
        "highBidderId": None,
        "highBidderName": None,
        # The leader's automatic-bidding ceiling. Never sent to clients.
        "proxyMax": None,
        "bidCount": 0,
        "watchCount": 0,
        "status": OPEN,
        "version": 1,
        # Bumped by every listing edit; a bid can pin the terms it was placed against.
        "termsVersion": 1,
        # Constant partition key for the byListing index ("all auctions, newest first").
        "listing": "ALL",
        # Partition key of the byEnding index; removed only by cancellation or takedown.
        "openListing": OPEN_LISTING,
        "price": data["startingPrice"],
        "searchText": search_text(data, seller["displayName"]),
        "createdAt": created,
        "updatedAt": created,
        "startsAt": data["startsAt"],
        "endsAt": data["endsAt"],
    }


def search_text(data: dict, seller_name: str) -> str:
    """Lower-cased words a search can match (DynamoDB `contains` is case-sensitive)."""
    parts = [data.get("title", ""), data.get("description", ""), data.get("category", "").replace("_", " "),
             seller_name or ""]
    return " ".join(" ".join(parts).lower().split())[:3000]


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def phase(item: dict, at_ms: int | None = None) -> str:
    """CANCELLED | ENDED | SCHEDULED | LIVE, from stored status plus the clock."""
    at = at_ms if at_ms is not None else now_ms()
    if item["status"] == CANCELLED:
        return "CANCELLED"
    if item["status"] == CLOSED or at >= int(item["endsAt"]):
        return "ENDED"
    if at < int(item.get("startsAt") or 0):
        return "SCHEDULED"
    return "LIVE"


def public_auction(item: dict, at_ms: int | None = None) -> dict:
    """Client-facing view. `isOpen` also accounts for an auction whose end time has
    passed but whose scheduled close hasn't run yet — bids are already rejected then."""
    at = at_ms if at_ms is not None else now_ms()
    item = {k: _plain(v) for k, v in item.items()}
    p = phase(item, at)
    return {
        "auctionId": item["auctionId"],
        "sellerId": item.get("sellerId"),
        "sellerName": item.get("sellerName"),
        "title": item["title"],
        "description": item.get("description", ""),
        "category": item.get("category", "OTHER"),
        "condition": item.get("condition", "GOOD"),
        "quantity": item.get("quantity", 1),
        "images": item.get("images") or [],
        "startingPrice": item["startingPrice"],
        "minIncrement": item["minIncrement"],
        "minNextBid": item["minNextBid"],
        "currentHigh": item.get("currentHigh"),
        # The reserve amount stays secret; bidders only learn whether it is met.
        "hasReserve": item.get("reservePrice") is not None,
        "reserveMet": reserve_met(item),
        # Offered only until the first bid.
        "buyNowPrice": item.get("buyNowPrice") if p in ("LIVE", "SCHEDULED") and not item.get("bidCount") else None,
        "soldVia": item.get("soldVia"),
        # Taken down by an admin (the reason is only shown to the seller and admins).
        "removed": bool(item.get("removedAt")),
        "watchCount": max(0, item.get("watchCount") or 0),
        "highBidderId": item.get("highBidderId"),
        "highBidderName": item.get("highBidderName"),
        "bidCount": item.get("bidCount", 0),
        "status": item["status"],
        "phase": p,
        "isOpen": p == "LIVE",
        "version": item["version"],
        "termsVersion": item.get("termsVersion", 1),
        "createdAt": item["createdAt"],
        "updatedAt": item["updatedAt"],
        "startsAt": item.get("startsAt", item["createdAt"]),
        "endsAt": item["endsAt"],
    }


def reserve_met(item: dict) -> bool:
    reserve = item.get("reservePrice")
    high = item.get("currentHigh")
    return high is not None and (reserve is None or int(high) >= int(reserve))


def winner_id(item: dict) -> str | None:
    """Who won an ended auction: the high bidder, provided the reserve was met."""
    return item.get("highBidderId") if item.get("status") == CLOSED and reserve_met(item) else None


def public_bid(item: dict) -> dict:
    item = {k: _plain(v) for k, v in item.items()}
    return {
        "bidId": item["bidId"],
        "auctionId": item["auctionId"],
        "bidderId": item["bidderId"],
        "bidderName": item["bidderName"],
        "amount": item["amount"],
        "placedAt": item["placedAt"],
        "auto": bool(item.get("auto")),
    }


# --------------------------------------------------------------------------- users

def parse_registration(body: Any) -> dict:
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    return {
        "displayName": _str(body.get("displayName"), "displayName", max_len=40),
        "requestSeller": bool(body.get("requestSeller", False)),
    }


def parse_profile_update(body: Any, owner_id: str) -> dict:
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    allowed = {"displayName", "bio", "location", "avatarKey", "emailNotifications"}
    unknown = set(body) - allowed
    if unknown:
        raise ValidationError(f"cannot change: {', '.join(sorted(unknown))}")
    out: dict = {}
    if "displayName" in body:
        out["displayName"] = _str(body["displayName"], "displayName", max_len=40)
    if "bio" in body:
        out["bio"] = _str(body["bio"], "bio", max_len=500, required=False)
    if "location" in body:
        out["location"] = _str(body["location"], "location", max_len=80, required=False)
    if "avatarKey" in body:
        out["avatarKey"] = None if body["avatarKey"] is None else image_key(body["avatarKey"], "avatarKey", owner_id)
    if "emailNotifications" in body:
        if not isinstance(body["emailNotifications"], bool):
            raise ValidationError("emailNotifications must be true or false")
        out["emailNotifications"] = body["emailNotifications"]
    if not out:
        raise ValidationError("nothing to change")
    return out


def parse_removal(body: Any) -> str:
    """Why an admin took a listing down. Shown to the seller, so it must say something."""
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    return _str(body.get("reason"), "reason", max_len=500)


def parse_approval(body: Any) -> dict:
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    out = {}
    for field in ("buyerStatus", "sellerStatus"):
        if field in body:
            out[field] = _choice(body[field], field, APPROVAL_STATES)
    if not out:
        raise ValidationError("set buyerStatus and/or sellerStatus")
    return out


def public_user(item: dict) -> dict:
    item = {k: _plain(v) for k, v in item.items()}
    return {
        "userId": item["userId"],
        "displayName": item["displayName"],
        "bio": item.get("bio", ""),
        "location": item.get("location", ""),
        "avatarKey": item.get("avatarKey"),
        "isSeller": item.get("sellerStatus") == APPROVED,
        "createdAt": item["createdAt"],
    }


def private_user(item: dict, *, is_admin: bool) -> dict:
    """What a user sees about themselves (and what admins see in the approval queue)."""
    item = {k: _plain(v) for k, v in item.items()}
    return {
        **public_user(item),
        "email": item.get("email", ""),
        "buyerStatus": item.get("buyerStatus", NONE),
        "sellerStatus": item.get("sellerStatus", NONE),
        "emailNotifications": item.get("emailNotifications", True),
        "isAdmin": is_admin,
    }
