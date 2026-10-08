"""Create (or reset) the demo admin account for judges.

    python scripts/create_demo_accounts.py --stack auction-backend-staging

A confirmed Cognito user with a known password, plus a BidBloom account that is
already approved to buy and sell. Safe to re-run: the password and approvals are
reset. It uses the reserved demo domain `bidbloom.demo`, which can't receive mail;
the notifier never sends to it. Its email must be in the stack's AdminEmails
parameter for it to have admin rights.
"""
from __future__ import annotations

import argparse
import time

import boto3

ACCOUNTS = [
    # email, password, display name
    ("judge.admin@bidbloom.demo", "Judge-admin-2026", "Judge Admin"),
]


def stack_outputs_and_tables(cfn, stack: str) -> tuple[dict, str]:
    desc = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in desc.get("Outputs", [])}
    users_table = cfn.describe_stack_resource(StackName=stack, LogicalResourceId="UsersTable")[
        "StackResourceDetail"]["PhysicalResourceId"]
    admins = next((p["ParameterValue"] for p in desc.get("Parameters", []) if p["ParameterKey"] == "AdminEmails"), "")
    outputs["AdminEmails"] = admins
    return outputs, users_table


def ensure_cognito_user(cog, pool: str, email: str, password: str) -> str:
    try:
        user = cog.admin_create_user(
            UserPoolId=pool, Username=email, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
        )["User"]
        attrs = user["Attributes"]
    except cog.exceptions.UsernameExistsException:
        attrs = cog.admin_get_user(UserPoolId=pool, Username=email)["UserAttributes"]
    cog.admin_set_user_password(UserPoolId=pool, Username=email, Password=password, Permanent=True)
    return next(a["Value"] for a in attrs if a["Name"] == "sub")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", required=True)
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    cfn = boto3.client("cloudformation", region_name=args.region)
    cog = boto3.client("cognito-idp", region_name=args.region)
    outputs, users_table = stack_outputs_and_tables(cfn, args.stack)
    pool = outputs["UserPoolId"]
    admins = {e.strip().lower() for e in outputs["AdminEmails"].split(",") if e.strip()}
    table = boto3.resource("dynamodb", region_name=args.region).Table(users_table)

    for email, password, name in ACCOUNTS:
        user_id = ensure_cognito_user(cog, pool, email, password)
        now = int(time.time() * 1000)
        # Create the BidBloom account if missing, and (re)approve it either way.
        table.update_item(
            Key={"userId": user_id},
            UpdateExpression=("SET email = :email, displayName = if_not_exists(displayName, :name), "
                              "bio = if_not_exists(bio, :empty), #loc = if_not_exists(#loc, :empty), "
                              "avatarKey = if_not_exists(avatarKey, :none), createdAt = if_not_exists(createdAt, :now), "
                              "buyerStatus = :approved, sellerStatus = :seller, updatedAt = :now"),
            ExpressionAttributeNames={"#loc": "location"},
            ExpressionAttributeValues={":email": email, ":name": name, ":empty": "", ":none": None, ":now": now,
                                       ":approved": "APPROVED", ":seller": "APPROVED"},
        )
        print(f"  {email}  /  {password}")
        if email not in admins:
            print(f"  note: {email} is not in AdminEmails, so it has no admin rights on this stack yet")


if __name__ == "__main__":
    main()
