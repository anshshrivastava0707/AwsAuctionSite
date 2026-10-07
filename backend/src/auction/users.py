"""User accounts and buyer/seller approval.

A user row is created on first login ("onboarding"). Each role has its own
approval state (NONE -> PENDING -> APPROVED | REJECTED) that only an admin can
change. Bidding requires buyerStatus = APPROVED and listing requires
sellerStatus = APPROVED; the bid check happens *inside* the bid transaction
(repository.place_bid), so revoking approval takes effect atomically.
"""
from __future__ import annotations

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from . import config
from .auth import Identity
from .errors import Conflict
from .models import APPROVED, NONE, PENDING, REJECTED, now_ms


def _table():
    return config.dynamodb_resource().Table(config.users_table_name())


def get_user(user_id: str) -> dict | None:
    return _table().get_item(Key={"userId": user_id}, ConsistentRead=True).get("Item")


def create_user(identity: Identity, display_name: str, request_seller: bool) -> dict:
    now = now_ms()
    # Admins are approved for both roles on creation; everyone else waits for one.
    auto = identity.is_admin
    item = {
        "userId": identity.user_id,
        "email": identity.email,
        "displayName": display_name,
        "bio": "",
        "location": "",
        "avatarKey": None,
        "buyerStatus": APPROVED if auto else PENDING,
        "sellerStatus": APPROVED if auto else (PENDING if request_seller else NONE),
        "createdAt": now,
        "updatedAt": now,
    }
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(userId)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("account already exists") from e
        raise
    return item


def update_profile(user_id: str, changes: dict) -> dict:
    names = {f"#{k}": k for k in changes}
    values = {f":{k}": v for k, v in changes.items()}
    sets = ", ".join(f"#{k} = :{k}" for k in changes)
    try:
        return _table().update_item(
            Key={"userId": user_id},
            UpdateExpression=f"SET {sets}, updatedAt = :now",
            ConditionExpression="attribute_exists(userId)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={**values, ":now": now_ms()},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("no account") from e
        raise


def request_seller(user_id: str) -> dict:
    """NONE or REJECTED -> PENDING. A pending or approved seller is left alone."""
    try:
        return _table().update_item(
            Key={"userId": user_id},
            UpdateExpression="SET sellerStatus = :pending, updatedAt = :now",
            ConditionExpression="attribute_exists(userId) AND sellerStatus IN (:none, :rejected)",
            ExpressionAttributeValues={":pending": PENDING, ":none": NONE, ":rejected": REJECTED,
                                       ":now": now_ms()},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("seller access is already requested or granted") from e
        raise


def set_approval(user_id: str, changes: dict) -> dict:
    return update_profile(user_id, changes)


def pending_users(limit: int = 100) -> list[dict]:
    # Small table and an admin-only page: a filtered Scan is fine here.
    resp = _table().scan(
        FilterExpression=Attr("buyerStatus").eq(PENDING) | Attr("sellerStatus").eq(PENDING),
    )
    items = sorted(resp.get("Items", []), key=lambda i: i["createdAt"])
    return items[:limit]


def all_users(limit: int = 200) -> list[dict]:
    items = sorted(_table().scan().get("Items", []), key=lambda i: i["createdAt"], reverse=True)
    return items[:limit]
