"""One-shot EventBridge Scheduler job per auction that flips it to CLOSED at endsAt.

Correctness doesn't depend on this firing on time: the bid condition already
rejects anything with now >= endsAt. The job just makes the CLOSED state
explicit and, via the stream, broadcasts it.
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


def _configured() -> tuple[str, str] | None:
    role_arn = os.environ.get("SCHEDULER_ROLE_ARN")
    target_arn = os.environ.get("CLOSE_FUNCTION_ARN")
    return (role_arn, target_arn) if role_arn and target_arn else None


def schedule_close(auction_id: str, ends_at_ms: int) -> None:
    """Create the close job, or move it if it exists (an edited end time)."""
    cfg = _configured()
    if cfg is None:
        log.warning("scheduler not configured; auction %s will not be auto-closed", auction_id)
        return
    role_arn, target_arn = cfg
    at = datetime.fromtimestamp(ends_at_ms / 1000, tz=timezone.utc)
    args = dict(
        Name=_name(auction_id),
        GroupName=_group(),
        ScheduleExpression=f"at({at.strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": target_arn,
            "RoleArn": role_arn,
            "Input": json.dumps({"auctionId": auction_id}),
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


def cancel_close(auction_id: str) -> None:
    if _configured() is None:
        return
    try:
        config.scheduler_client().delete_schedule(Name=_name(auction_id), GroupName=_group())
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
