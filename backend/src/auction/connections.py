"""WebSocket connection registry and fan-out.

Connections are pure *routing* data. Nothing here ever writes auction or bid
state, so a client disconnecting (cleanly or not) can't corrupt shared state —
the worst case is a stale row, which is pruned on the next failed send (410
Gone) or by the table's TTL.

Rows, keyed by (connectionId, sk):
  sk = "META"           who is on this connection (userId, absent = anonymous viewer)
  sk = "SUB#<auctionId>" one per auction the connection follows; the byAuction
                         index over `auctionId` is the broadcast fan-out list.
A connection can follow several auctions (the "My bids" page watches all of them).
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from . import config
from .models import now_ms

log = logging.getLogger(__name__)

# API Gateway caps a WebSocket connection at 2 hours; keep rows a little longer
# so TTL only ever removes connections that are definitely dead.
CONNECTION_TTL_SECONDS = 3 * 3600
MAX_SUBSCRIPTIONS = 100  # per connection
META = "META"


def _table():
    return config.dynamodb_resource().Table(config.connections_table_name())


def _ttl(now: int) -> int:
    return now // 1000 + CONNECTION_TTL_SECONDS


def register(connection_id: str, user_id: str | None = None) -> None:
    """`user_id` is the identity proven at $connect (None = anonymous viewer)."""
    now = now_ms()
    item = {"connectionId": connection_id, "sk": META, "connectedAt": now, "ttl": _ttl(now)}
    if user_id:
        item["userId"] = user_id
    _table().put_item(Item=item)


def get(connection_id: str) -> dict | None:
    return _table().get_item(Key={"connectionId": connection_id, "sk": META}, ConsistentRead=True).get("Item")


def subscribe(connection_id: str, auction_id: str) -> None:
    subscribe_many(connection_id, [auction_id])


def subscribe_many(connection_id: str, auction_ids: list[str]) -> None:
    """Adds subscriptions (existing ones are kept). Separate rows from META, so the
    identity bound at $connect is never overwritten by a subscribe."""
    ids = list(dict.fromkeys(auction_ids))
    if len(ids) > MAX_SUBSCRIPTIONS:
        raise ValueError(f"at most {MAX_SUBSCRIPTIONS} auctions per connection")
    now = now_ms()
    with _table().batch_writer() as batch:
        for aid in ids:
            batch.put_item(Item={"connectionId": connection_id, "sk": f"SUB#{aid}", "auctionId": aid,
                                 "connectedAt": now, "ttl": _ttl(now)})


def remove(connection_id: str) -> None:
    """Deletes the connection's META row and all of its subscriptions."""
    table = _table()
    kwargs = {"KeyConditionExpression": Key("connectionId").eq(connection_id), "ProjectionExpression": "sk"}
    keys: list[str] = []
    while True:
        resp = table.query(**kwargs)
        keys.extend(i["sk"] for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    with table.batch_writer() as batch:
        for sk in keys:
            batch.delete_item(Key={"connectionId": connection_id, "sk": sk})


def subscribers(auction_id: str) -> list[str]:
    ids: list[str] = []
    kwargs = {"IndexName": "byAuction", "KeyConditionExpression": Key("auctionId").eq(auction_id)}
    while True:
        resp = _table().query(**kwargs)
        ids.extend(i["connectionId"] for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return list(dict.fromkeys(ids))
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def send(endpoint: str, connection_id: str, payload: dict) -> bool:
    """Send one message. Returns False (and prunes the row) if the connection is gone."""
    client = config.apigw_management_client(endpoint)
    try:
        client.post_to_connection(ConnectionId=connection_id, Data=json.dumps(payload).encode())
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code == "GoneException" or status == 410:
            remove(connection_id)
            return False
        log.warning("send to %s failed: %s", connection_id, code)
        return False


def broadcast(endpoint: str, auction_id: str, payload: dict, max_workers: int = 16) -> dict:
    targets = subscribers(auction_id)
    if not targets:
        return {"sent": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as pool:
        results = list(pool.map(lambda cid: send(endpoint, cid, payload), targets))
    sent = sum(results)
    return {"sent": sent, "failed": len(results) - sent}
