"""Invoked by EventBridge Scheduler at an auction's endsAt to mark it CLOSED."""
from __future__ import annotations

import logging
import time

from auction import repository, schedules
from auction.models import now_ms

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    auction_id = event["auctionId"]
    for _ in range(3):
        auction = repository.get_auction(auction_id)
        if auction is None or auction["status"] != "OPEN":
            return {"closed": False}
        ends_at = int(auction["endsAt"])
        wait_ms = ends_at - now_ms()
        if wait_ms > 60_000:
            # The end moved later: a seller edit, or a last-minute bid extended it.
            # Make sure a close job exists for the new end, then stop.
            schedules.schedule_close(auction_id, ends_at, target_arn=getattr(context, "invoked_function_arn", None),
                                     follow_up=True)
            return {"closed": False, "rescheduled": ends_at}
        if wait_ms > 0:
            # Scheduler can fire up to a second or so early; wait it out.
            time.sleep(wait_ms / 1000 + 0.05)
        if repository.close_auction(auction_id):
            log.info("closed %s", auction_id)
            return {"closed": True}
        # Not closed: a bid in the final moments extended the end. Look again.
    raise RuntimeError(f"auction {auction_id} could not be closed yet")  # Scheduler retries
