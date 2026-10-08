"""One-shot EventBridge Scheduler jobs per auction.

  close-<id>      at endsAt, flips the auction to CLOSED (handlers/scheduler.py).
                  Correctness doesn't depend on this firing on time: the bid
                  condition already rejects anything with now >= endsAt. The job
                  just makes the CLOSED state explicit and, via the stream,
                  broadcasts it and sends the result emails.
  close-<id>-x<t> a follow-up close after late bids extended the end (soft close).
  remind-<id>     an hour before endsAt, "ending soon" emails (handlers/notify.py).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from . import config

log = logging.getLogger(__name__)


def _name(auction_id: str) -> str:
    return f"close-{auction_id}"


def _group() -> str:
    return os.environ.get("SCHEDULE_GROUP", "default")


REMINDER_LEAD_MS = 3600 * 1000


def _role() -> str | None:
    return os.environ.get("SCHEDULER_ROLE_ARN") or None


def _put(name: str, at_ms: int, target_arn: str, payload: dict) -> None:
    """Create the job, or move it if it already exists."""
    at = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc)
    args = dict(
        Name=name,
        GroupName=_group(),
        ScheduleExpression=f"at({at.strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": target_arn,
            "RoleArn": _role(),
            "Input": json.dumps(payload),
            "RetryPolicy": {"MaximumRetryAttempts": 10, "MaximumEventAgeInSeconds": 3600},
        },
    )
    client = config.scheduler_client()
    try:
        client.create_schedule(**args)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        client.update_schedule(**args)


def _delete(name: str) -> None:
    try:
        config.scheduler_client().delete_schedule(Name=name, GroupName=_group())
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise


def schedule_close(auction_id: str, ends_at_ms: int, *, target_arn: str | None = None,
                   follow_up: bool = False) -> None:
    """`follow_up` makes a separately named job, so the close job that is running
    right now (and deletes itself when done) is never edited mid-flight."""
    target = target_arn or os.environ.get("CLOSE_FUNCTION_ARN")
    if not (_role() and target):
        log.warning("scheduler not configured; auction %s will not be auto-closed", auction_id)
        return
    name = f"{_name(auction_id)}-x{ends_at_ms}" if follow_up else _name(auction_id)
    _put(name, ends_at_ms, target, {"auctionId": auction_id})


def schedule_reminder(auction_id: str, ends_at_ms: int, at_ms: int) -> None:
    """"Ending soon" emails an hour before the end; none for auctions shorter than that."""
    target = os.environ.get("NOTIFY_FUNCTION_ARN")
    if not (_role() and target):
        return
    when = ends_at_ms - REMINDER_LEAD_MS
    if when < at_ms + 60_000:
        _delete(f"remind-{auction_id}")
        return
    _put(f"remind-{auction_id}", when, target, {"kind": "endingSoon", "auctionId": auction_id})


def cancel_close(auction_id: str) -> None:
    if not _role():
        return
    _delete(_name(auction_id))
    _delete(f"remind-{auction_id}")
