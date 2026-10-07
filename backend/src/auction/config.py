"""Environment-driven configuration and lazily created AWS clients.

Clients are created on first use (not at import time) so tests can patch the
environment / mock AWS before anything touches boto3.
"""
import os
from functools import lru_cache

import boto3


def auctions_table_name() -> str:
    return os.environ["AUCTIONS_TABLE"]


def bids_table_name() -> str:
    return os.environ["BIDS_TABLE"]


def connections_table_name() -> str:
    return os.environ["CONNECTIONS_TABLE"]


def users_table_name() -> str:
    return os.environ["USERS_TABLE"]


def images_bucket() -> str:
    return os.environ.get("IMAGES_BUCKET", "")


@lru_cache(maxsize=None)
def dynamodb_client():
    return boto3.client("dynamodb")


@lru_cache(maxsize=None)
def dynamodb_resource():
    return boto3.resource("dynamodb")


@lru_cache(maxsize=None)
def s3_client():
    return boto3.client("s3")


@lru_cache(maxsize=None)
def scheduler_client():
    return boto3.client("scheduler")


@lru_cache(maxsize=None)
def apigw_management_client(endpoint_url: str):
    return boto3.client("apigatewaymanagementapi", endpoint_url=endpoint_url)


def reset_clients() -> None:
    """Used by tests to drop cached clients between mocked AWS sessions."""
    dynamodb_client.cache_clear()
    dynamodb_resource.cache_clear()
    scheduler_client.cache_clear()
    s3_client.cache_clear()
    apigw_management_client.cache_clear()
