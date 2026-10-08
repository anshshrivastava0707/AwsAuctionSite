"""DynamoDB Stream consumer on the Auctions table -> WebSocket broadcast.

Broadcasting from the stream (rather than from the bid handler) guarantees
clients are only ever told about state that is durably committed, in commit
order per auction. If this function errors, the stream retries the batch and
clients may get a duplicate, which they drop by `version`.
"""
from __future__ import annotations

import logging
import os

from boto3.dynamodb.types import TypeDeserializer

from auction import connections
from auction.models import _plain, public_auction

log = logging.getLogger()
log.setLevel(logging.INFO)
_deser = TypeDeserializer()


def handler(event, _context):
    endpoint = os.environ["WS_ENDPOINT"]
    for record in event.get("Records", []):
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            continue
        image = record["dynamodb"].get("NewImage")
        if not image:
            continue
        item = {k: _deser.deserialize(v) for k, v in image.items()}
        old = record["dynamodb"].get("OldImage")
        if old and old.get("version") == image.get("version"):
            continue  # only watchCount moved (a save); clients would drop it by version anyway
        auction = public_auction(item)

        payload: dict = {"type": "auctionUpdate", "auction": auction}
        old_high = _deser.deserialize(old["currentHigh"]) if old and "currentHigh" in old else None
        if auction["currentHigh"] is not None and auction["currentHigh"] != old_high:
            # The new high bid is fully described by the auction row, so clients can
            # append it to their history without another read.
            payload["bid"] = {
                "auctionId": auction["auctionId"],
                "bidderId": auction["highBidderId"],
                "bidderName": auction["highBidderName"],
                "amount": auction["currentHigh"],
                "placedAt": auction["updatedAt"],
            }
        old_count = int(_deser.deserialize(old["bidCount"])) if old and "bidCount" in old else 0
        if item.get("lastBids") and int(item.get("bidCount") or 0) != old_count:
            # Every bid row this commit wrote, including automatic bids and a
            # challenger who was immediately outbid.
            payload["bids"] = [{k: _plain(v) for k, v in b.items()} for b in item["lastBids"]]
        stats = connections.broadcast(endpoint, auction["auctionId"], payload)
        log.info("broadcast %s v%s: %s", auction["auctionId"], auction["version"], stats)
    return {"ok": True}
