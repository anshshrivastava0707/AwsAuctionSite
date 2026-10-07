import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from auction import config  # noqa: E402
from devtools import tables  # noqa: E402

ADMIN_EMAIL = "admin@example.com"

os.environ.update({
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AUCTIONS_TABLE": "auctions",
    "BIDS_TABLE": "bids",
    "CONNECTIONS_TABLE": "connections",
    "USERS_TABLE": "users",
    "IMAGES_BUCKET": "images",
    "AUTH_MODE": "dev",
    "DEV_AUTH_SECRET": "test-secret-0123456789",
    "ADMIN_EMAILS": ADMIN_EMAIL,
})
for _var in ("SCHEDULER_ROLE_ARN", "CLOSE_FUNCTION_ARN"):
    os.environ.pop(_var, None)


@pytest.fixture
def aws():
    with mock_aws():
        config.reset_clients()
        tables.create_all(boto3.client("dynamodb"))
        boto3.client("s3").create_bucket(Bucket="images")
        yield
        config.reset_clients()


def make_user(name: str, *, buyer: str = "APPROVED", seller: str = "APPROVED") -> dict:
    """Creates an account directly in the table (bypassing approval) and returns
    it with a valid dev-mode login token under `token`."""
    from auction import auth, users

    email = f"{name.lower()}@example.com"
    identity = auth.Identity(user_id=auth.dev_user_id(email), email=email)
    item = users.create_user(identity, name.title(), request_seller=False)
    item = users.set_approval(identity.user_id, {"buyerStatus": buyer, "sellerStatus": seller})
    return {**item, "token": auth.issue_dev_token(email)["token"]}


@pytest.fixture
def seller(aws):
    return make_user("seller")


@pytest.fixture
def auction(seller):
    from auction import repository
    from auction.models import new_auction_item

    item = new_auction_item({
        "title": "Test item", "description": "", "category": "OTHER", "condition": "GOOD",
        "quantity": 1, "images": [], "startingPrice": 1000, "minIncrement": 100,
        "startsAt": 0, "endsAt": 0,
    }, seller)
    item["startsAt"] = item["createdAt"]
    item["endsAt"] = item["createdAt"] + 600_000
    repository.create_auction(item)
    return item
