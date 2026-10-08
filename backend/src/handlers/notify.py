"""Sends notification emails (auction/notify.py).

Two triggers:
  - the Auctions table stream (a second consumer next to the broadcaster), for
    "outbid" and "auction closed", seen as before/after images of the row;
  - EventBridge Scheduler, {"kind": "endingSoon", "auctionId": ...}, an hour
    before an auction ends.
"""
from __future__ import annotations

import logging

from boto3.dynamodb.types import TypeDeserializer

from auction import notify, repository
from auction.models import CLOSED, OPEN, phase

log = logging.getLogger()
log.setLevel(logging.INFO)
_deser = TypeDeserializer()


def _image(record: dict, which: str) -> dict | None:
    image = record["dynamodb"].get(which)
    return {k: _deser.deserialize(v) for k, v in image.items()} if image else None


def handler(event, _context):
    if event.get("kind") == "endingSoon":
        auction = repository.get_auction(event["auctionId"])
        if auction and auction["status"] == OPEN and phase(auction) == "LIVE":
            notify.ending_soon(auction)
        return {"ok": True}

    for record in event.get("Records", []):
        if record.get("eventName") != "MODIFY":
            continue
        old, new = _image(record, "OldImage"), _image(record, "NewImage")
        if not old or not new:
            continue
        if new.get("removedAt") and not old.get("removedAt"):
            notify.removed(new)
            continue
        before, after = old.get("highBidderId"), new.get("highBidderId")
        if before and after != before and new["status"] in (OPEN, CLOSED):
            notify.outbid(new, before)
        if old["status"] == OPEN and new["status"] == CLOSED:
            notify.closed(new)
    return {"ok": True}
