"""Invoked by EventBridge Scheduler at an auction's endsAt to mark it CLOSED."""
from __future__ import annotations

import logging
import time

from auction import repository
from auction.models import now_ms

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, _context):
    auction_id = event["auctionId"]
    auction = repository.get_auction(auction_id)
    if auction is None or auction["status"] != "OPEN":
        return {"closed": False}
    # Scheduler can fire up to a second or so early; wait it out rather than
    # failing the conditional close.
    wait_ms = int(auction["endsAt"]) - now_ms()
    if wait_ms > 60_000:
        # The seller moved the end time later; a newer schedule will close it.
        return {"closed": False, "rescheduled": True}
    if 0 < wait_ms <= 60_000:
        time.sleep(wait_ms / 1000 + 0.05)
    closed = repository.close_auction(auction_id)
    if not closed and int(auction["endsAt"]) > now_ms():
        # Still too early (should not happen); raise so Scheduler retries.
        raise RuntimeError(f"auction {auction_id} not yet ended")
    log.info("close %s -> %s", auction_id, closed)
    return {"closed": closed}
