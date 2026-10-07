"""DynamoDB table definitions for the offline stand-ins (tests and scripts/local_server.py).

They must mirror backend/template.yaml; tests/test_tables.py fails if they drift.
"""
from __future__ import annotations


def _attrs(**types: str) -> list[dict]:
    return [{"AttributeName": n, "AttributeType": t} for n, t in types.items()]


def _keys(hash_key: str, range_key: str | None = None) -> list[dict]:
    keys = [{"AttributeName": hash_key, "KeyType": "HASH"}]
    if range_key:
        keys.append({"AttributeName": range_key, "KeyType": "RANGE"})
    return keys


def _index(name: str, hash_key: str, range_key: str | None = None, projection: str = "ALL") -> dict:
    return {"IndexName": name, "KeySchema": _keys(hash_key, range_key), "Projection": {"ProjectionType": projection}}


def definitions(prefix: str = "") -> dict[str, dict]:
    """Logical name (as in template.yaml) -> create_table kwargs."""
    return {
        "AuctionsTable": {
            "TableName": f"{prefix}auctions",
            "AttributeDefinitions": _attrs(auctionId="S", listing="S", category="S", sellerId="S", createdAt="N"),
            "KeySchema": _keys("auctionId"),
            "GlobalSecondaryIndexes": [
                _index("byListing", "listing", "createdAt"),
                _index("byCategory", "category", "createdAt"),
                _index("bySeller", "sellerId", "createdAt"),
            ],
        },
        "BidsTable": {
            "TableName": f"{prefix}bids",
            "AttributeDefinitions": _attrs(auctionId="S", bidId="S", amount="N", bidderId="S", placedAt="N"),
            "KeySchema": _keys("auctionId", "bidId"),
            "LocalSecondaryIndexes": [_index("byAmount", "auctionId", "amount")],
            "GlobalSecondaryIndexes": [_index("byBidder", "bidderId", "placedAt")],
        },
        "ConnectionsTable": {
            "TableName": f"{prefix}connections",
            "AttributeDefinitions": _attrs(connectionId="S", sk="S", auctionId="S"),
            "KeySchema": _keys("connectionId", "sk"),
            "GlobalSecondaryIndexes": [_index("byAuction", "auctionId", projection="KEYS_ONLY")],
        },
        "UsersTable": {
            "TableName": f"{prefix}users",
            "AttributeDefinitions": _attrs(userId="S"),
            "KeySchema": _keys("userId"),
        },
    }


ENV_VARS = {
    "AuctionsTable": "AUCTIONS_TABLE",
    "BidsTable": "BIDS_TABLE",
    "ConnectionsTable": "CONNECTIONS_TABLE",
    "UsersTable": "USERS_TABLE",
}


def create_all(client, prefix: str = "") -> dict[str, str]:
    """Creates every table; returns {ENV_VAR: table name} to put in os.environ."""
    env = {}
    for logical, spec in definitions(prefix).items():
        client.create_table(BillingMode="PAY_PER_REQUEST", **spec)
        env[ENV_VARS[logical]] = spec["TableName"]
    return env
