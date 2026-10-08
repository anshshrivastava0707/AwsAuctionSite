import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

from auction import auth, connections, repository
from conftest import ADMIN_EMAIL, make_user
from handlers import http, scheduler, stream, ws


def ws_event(route, connection_id="c1", body=None, token=None):
    return {
        "requestContext": {"routeKey": route, "connectionId": connection_id,
                           "domainName": "abc.execute-api.us-east-1.amazonaws.com", "stage": "dev"},
        "queryStringParameters": {"token": token} if token else None,
        "body": json.dumps(body) if body is not None else None,
    }


def call(method, path, body=None, token=None, query=None):
    """Invoke the HTTP handler with an API Gateway v2 event; returns (status, json)."""
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"authorization": f"Bearer {token}"} if token else {},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }
    r = http.handler(event, None)
    return r["statusCode"], (json.loads(r["body"]) if r.get("body") else None)


class FakeApiGw:
    """Captures post_to_connection calls; connections in `gone` raise 410."""

    def __init__(self, gone=()):
        self.sent: dict[str, list[dict]] = {}
        self.gone = set(gone)

    def post_to_connection(self, ConnectionId, Data):
        if ConnectionId in self.gone:
            raise ClientError({"Error": {"Code": "GoneException"},
                               "ResponseMetadata": {"HTTPStatusCode": 410}}, "PostToConnection")
        self.sent.setdefault(ConnectionId, []).append(json.loads(Data))


def place(aid, bid_id, amount, **extra):
    return {"action": "placeBid", "auctionId": aid, "bidId": bid_id, "amount": amount, **extra}


# --------------------------------------------------------------------------- WebSocket

def test_connect_subscribe_snapshot_bid_and_disconnect(auction):
    alice = make_user("alice")
    gw = FakeApiGw()
    with patch("auction.config.apigw_management_client", return_value=gw):
        ws.handler(ws_event("$connect", token=alice["token"]), None)
        ws.handler(ws_event("subscribe", body={"action": "subscribe", "auctionId": auction["auctionId"]}), None)
        assert connections.subscribers(auction["auctionId"]) == ["c1"]
        assert connections.get("c1")["userId"] == alice["userId"]  # subscribe kept the identity
        snap = gw.sent["c1"][-1]
        assert snap["type"] == "snapshot" and snap["auction"]["version"] == 1

        # A spoofed bidderId in the message is ignored: the bid belongs to the connection's user.
        ws.handler(ws_event("placeBid", body={**place(auction["auctionId"], "b1", 1000), "bidderId": "evil"}), None)
        result = gw.sent["c1"][-1]
        assert result["type"] == "bidResult" and result["status"] == "ACCEPTED"
        assert result["bid"]["bidderId"] == alice["userId"] and result["bid"]["bidderName"] == "Alice"

        before = repository.get_auction(auction["auctionId"])
        ws.handler(ws_event("$disconnect"), None)
        assert connections.subscribers(auction["auctionId"]) == []
        # Disconnecting touches routing data only — auction state is unchanged.
        assert repository.get_auction(auction["auctionId"]) == before


def test_anonymous_viewer_can_watch_but_not_bid(auction):
    gw = FakeApiGw()
    with patch("auction.config.apigw_management_client", return_value=gw):
        ws.handler(ws_event("$connect", token="forged.token"), None)  # bad token -> anonymous
        ws.handler(ws_event("subscribe", body={"action": "subscribe", "auctionId": auction["auctionId"]}), None)
        assert gw.sent["c1"][-1]["type"] == "snapshot"
        ws.handler(ws_event("placeBid", body=place(auction["auctionId"], "b1", 1000)), None)
    assert gw.sent["c1"][-1]["reason"] == "NOT_AUTHENTICATED"
    assert int(repository.get_auction(auction["auctionId"])["bidCount"]) == 0


def test_logged_in_without_account_is_told_to_finish_setup(auction):
    token = auth.issue_dev_token("ghost@example.com")["token"]  # valid login, no account row
    gw = FakeApiGw()
    with patch("auction.config.apigw_management_client", return_value=gw):
        ws.handler(ws_event("$connect", token=token), None)
        ws.handler(ws_event("placeBid", body=place(auction["auctionId"], "b1", 1000)), None)
    assert gw.sent["c1"][-1]["reason"] == "NOT_APPROVED"


def test_reconnect_with_same_bid_after_drop_is_not_double_counted(auction):
    """Client sends a bid, the socket drops before the reply arrives, the client
    reconnects (new connectionId) and re-sends the same bidId."""
    alice = make_user("alice")
    gw = FakeApiGw(gone={"old"})
    msg = place(auction["auctionId"], "b-retry", 1500)
    with patch("auction.config.apigw_management_client", return_value=gw):
        ws.handler(ws_event("$connect", "old", token=alice["token"]), None)
        ws.handler(ws_event("placeBid", "old", msg), None)  # committed, reply lost (410)
        ws.handler(ws_event("$disconnect", "old"), None)
        ws.handler(ws_event("$connect", "new", token=alice["token"]), None)
        ws.handler(ws_event("subscribe", "new", {"action": "subscribe", "auctionId": auction["auctionId"]}), None)
        ws.handler(ws_event("placeBid", "new", msg), None)
    reply = gw.sent["new"][-1]
    assert reply["status"] == "ACCEPTED" and reply["duplicate"] is True
    state = repository.get_auction(auction["auctionId"])
    assert int(state["bidCount"]) == 1 and int(state["currentHigh"]) == 1500


def test_invalid_messages_get_error_reply(auction):
    gw = FakeApiGw()
    with patch("auction.config.apigw_management_client", return_value=gw):
        ws.handler(ws_event("$connect", token=make_user("u")["token"]), None)
        ws.handler(ws_event("placeBid", body={**place(auction["auctionId"], "b", 1), "amount": "lots"}), None)
        ev = ws_event("$default")
        ev["body"] = "not json"
        ws.handler(ev, None)
    assert [m["type"] for m in gw.sent["c1"]] == ["error", "error"]
    assert int(repository.get_auction(auction["auctionId"])["version"]) == 1


def test_ping_reports_version(auction):
    gw = FakeApiGw()
    with patch("auction.config.apigw_management_client", return_value=gw):
        ws.handler(ws_event("ping", body={"action": "ping", "auctionId": auction["auctionId"]}), None)
    assert gw.sent["c1"][-1] == {"type": "pong", "auctionId": auction["auctionId"], "version": 1}


def test_broadcast_prunes_gone_connections(auction):
    aid = auction["auctionId"]
    for cid in ("alive", "dead"):
        connections.subscribe(cid, aid)
    gw = FakeApiGw(gone={"dead"})
    from boto3.dynamodb.types import TypeSerializer
    ser = TypeSerializer()
    new = {**auction, "currentHigh": 1000, "highBidderId": "u", "highBidderName": "U",
           "version": 2, "minNextBid": 1100, "bidCount": 1}
    record = {"eventName": "MODIFY", "dynamodb": {
        "NewImage": {k: ser.serialize(v) for k, v in new.items()},
        "OldImage": {k: ser.serialize(v) for k, v in auction.items()},
    }}
    with patch("auction.config.apigw_management_client", return_value=gw), \
         patch.dict("os.environ", {"WS_ENDPOINT": "https://x/dev"}):
        stream.handler({"Records": [record]}, None)
    msg = gw.sent["alive"][0]
    assert msg["type"] == "auctionUpdate" and msg["auction"]["version"] == 2
    assert msg["bid"]["amount"] == 1000
    assert connections.subscribers(aid) == ["alive"]


# --------------------------------------------------------------------------- accounts & approval

def test_dev_login_onboarding_and_admin_approval(aws):
    status, login = call("POST", "/auth/dev-login", {"email": "Ann@Example.com"})
    assert status == 200 and login["userId"] == auth.dev_user_id("ann@example.com")
    token = login["token"]

    assert call("GET", "/me", token=token)[0] == 404  # logged in, no account yet
    status, body = call("POST", "/me", {"displayName": "Ann", "requestSeller": True}, token=token)
    assert status == 201 and body["user"]["buyerStatus"] == "PENDING" and body["user"]["sellerStatus"] == "PENDING"
    assert call("POST", "/me", {"displayName": "Ann"}, token=token)[0] == 409

    # Not approved yet -> can't sell.
    assert call("POST", "/auctions", {"title": "x", "startingPrice": 1, "durationSeconds": 60}, token=token)[0] == 403

    admin = auth.issue_dev_token(ADMIN_EMAIL)["token"]
    assert call("POST", "/me", {"displayName": "Admin"}, token=admin)[1]["user"]["buyerStatus"] == "APPROVED"
    assert call("GET", "/admin/users", token=token)[0] == 403  # Ann isn't an admin
    pending = call("GET", "/admin/users", token=admin)[1]["users"]
    assert [u["displayName"] for u in pending] == ["Ann"]
    status, body = call("POST", f"/admin/users/{login['userId']}",
                        {"buyerStatus": "APPROVED", "sellerStatus": "APPROVED"}, token=admin)
    assert status == 200 and body["user"]["sellerStatus"] == "APPROVED"
    assert call("GET", "/admin/users", token=admin)[1]["users"] == []

    status, body = call("POST", "/auctions", {"title": "Lamp", "startingPrice": 500, "durationSeconds": 120},
                        token=token)
    assert status == 201 and body["auction"]["sellerName"] == "Ann"


def test_auth_required_and_tokens_verified(aws):
    assert call("GET", "/me")[0] == 401
    assert call("GET", "/me", token="garbage")[0] == 401
    good = auth.issue_dev_token("x@example.com")["token"]
    payload, sig = good.split(".")
    assert call("GET", "/me", token=f"{payload}.{sig[:-2]}AA")[0] == 401  # tampered signature
    with patch("auction.auth.now_ms", return_value=10**15):
        assert call("GET", "/me", token=good)[0] == 401  # expired
    assert call("POST", "/auth/dev-login", {"email": "not-an-email"})[0] == 400


def test_provider_modes_are_not_implemented_yet(aws):
    with patch.dict("os.environ", {"AUTH_MODE": "cognito"}):
        assert call("POST", "/auth/dev-login", {"email": "a@example.com"})[0] == 404
        assert call("GET", "/me", token="anything")[0] == 501


def test_profile_update_and_public_profile(seller):
    status, body = call("PUT", "/me", {"bio": "Collector", "location": "Pune"}, token=seller["token"])
    assert status == 200 and body["user"]["bio"] == "Collector"
    status, body = call("GET", f"/users/{seller['userId']}")
    assert status == 200 and body["user"]["isSeller"] is True
    assert "email" not in body["user"] and "buyerStatus" not in body["user"]  # private fields stay private
    assert call("PUT", "/me", {"buyerStatus": "APPROVED"}, token=seller["token"])[0] == 400  # can't self-approve


def test_seller_request_flow(aws):
    u = make_user("bob", seller="NONE")
    status, body = call("POST", "/me/seller-request", token=u["token"])
    assert status == 200 and body["user"]["sellerStatus"] == "PENDING"
    assert call("POST", "/me/seller-request", token=u["token"])[0] == 409


# --------------------------------------------------------------------------- listings

def test_create_listing_with_all_fields_and_schedule(seller):
    sched = MagicMock()
    now = repository.now_ms()
    body = {"title": "Camera", "description": "Works", "category": "ELECTRONICS", "condition": "LIKE_NEW",
            "quantity": 2, "startingPrice": 500, "minIncrement": 50,
            "startsAt": now + 3_600_000, "endsAt": now + 7_200_000}
    with patch("auction.config.scheduler_client", return_value=sched), \
         patch.dict("os.environ", {"SCHEDULER_ROLE_ARN": "arn:role", "CLOSE_FUNCTION_ARN": "arn:fn"}):
        status, body = call("POST", "/auctions", body, token=seller["token"])
    assert status == 201
    a = body["auction"]
    assert (a["category"], a["condition"], a["quantity"], a["phase"]) == ("ELECTRONICS", "LIKE_NEW", 2, "SCHEDULED")
    assert sched.create_schedule.call_args.kwargs["Name"] == f"close-{a['auctionId']}"

    assert call("GET", f"/auctions/{a['auctionId']}")[1]["auction"]["title"] == "Camera"
    assert [x["auctionId"] for x in call("GET", "/auctions", query={"category": "ELECTRONICS"})[1]["auctions"]] \
        == [a["auctionId"]]
    assert call("GET", "/auctions", query={"category": "BOOKS"})[1]["auctions"] == []
    assert [x["auctionId"] for x in call("GET", f"/users/{seller['userId']}/auctions")[1]["auctions"]] \
        == [a["auctionId"]]


@pytest.mark.parametrize("bad", [
    {"title": "x"},
    {"title": "x", "startingPrice": 1, "durationSeconds": 60, "category": "WEAPONS"},
    {"title": "x", "startingPrice": 1, "durationSeconds": 60, "quantity": 0},
    {"title": "x", "startingPrice": 1, "startsAt": 0, "endsAt": 10**13},  # starts in the past
    {"title": "x", "startingPrice": 1, "durationSeconds": 60, "images": ["uploads/someone-else/" + "a" * 32 + ".jpg"]},
])
def test_create_listing_validation(seller, bad):
    assert call("POST", "/auctions", bad, token=seller["token"])[0] == 400


def test_listing_newest_first_and_cancelled_hidden(seller):
    ids = [call("POST", "/auctions", {"title": f"Item {i}", "startingPrice": 1, "durationSeconds": 60},
                token=seller["token"])[1]["auction"]["auctionId"] for i in range(3)]
    call("POST", f"/auctions/{ids[1]}/cancel", token=seller["token"])
    listed = [a["auctionId"] for a in call("GET", "/auctions")[1]["auctions"]]
    assert ids[1] not in listed and set(listed) == {ids[0], ids[2]}
    mine = {a["auctionId"]: a["phase"] for a in call("GET", "/me/auctions", token=seller["token"])[1]["auctions"]}
    assert mine[ids[1]] == "CANCELLED" and len(mine) == 3


def test_edit_and_cancel_rules_over_http(auction, seller):
    aid = auction["auctionId"]
    other = make_user("other")
    assert call("PATCH", f"/auctions/{aid}", {"title": "Nope"}, token=other["token"])[0] == 403
    status, body = call("PATCH", f"/auctions/{aid}", {"title": "Renamed"}, token=seller["token"])
    assert status == 200 and body["auction"]["title"] == "Renamed" and body["auction"]["termsVersion"] == 2
    assert call("POST", f"/auctions/{aid}/cancel", token=other["token"])[0] == 403

    repository.place_bid({"auctionId": aid, "bidId": "b1", "bidderId": other["userId"],
                          "bidderName": "Other", "amount": 1000})
    assert call("PATCH", f"/auctions/{aid}", {"title": "Late"}, token=seller["token"])[0] == 409
    assert call("POST", f"/auctions/{aid}/cancel", token=seller["token"])[0] == 409


def test_edit_end_time_moves_close_schedule(auction, seller):
    sched = MagicMock()
    sched.create_schedule.side_effect = ClientError({"Error": {"Code": "ConflictException"}}, "CreateSchedule")
    with patch("auction.config.scheduler_client", return_value=sched), \
         patch.dict("os.environ", {"SCHEDULER_ROLE_ARN": "arn:role", "CLOSE_FUNCTION_ARN": "arn:fn"}):
        status, _ = call("PATCH", f"/auctions/{auction['auctionId']}", {"endsAt": auction["endsAt"] + 60_000},
                         token=seller["token"])
        assert status == 200 and sched.update_schedule.called
        call("POST", f"/auctions/{auction['auctionId']}/cancel", token=seller["token"])
        assert sched.delete_schedule.called


def test_close_job_reschedules_when_end_moved_later(auction):
    # Fired at the original end, but the auction now ends later (an edit, or a
    # late bid extended it): don't close, re-schedule for the new end instead.
    assert scheduler.handler({"auctionId": auction["auctionId"]}, None) == {
        "closed": False, "rescheduled": auction["endsAt"]}


# --------------------------------------------------------------------------- history

def test_my_bids_grouped_by_auction(auction, seller):
    alice, bob = make_user("alice"), make_user("bob")
    for who, amount in ((alice, 1000), (bob, 1100), (alice, 1500)):
        assert repository.place_bid({"auctionId": auction["auctionId"], "bidId": f"b{amount}",
                                     "bidderId": who["userId"], "bidderName": who["displayName"],
                                     "amount": amount}).accepted
    entries = call("GET", "/me/bids", token=alice["token"])[1]["entries"]
    assert len(entries) == 1
    assert [b["amount"] for b in entries[0]["bids"]] == [1500, 1000]  # newest first, only mine
    assert entries[0]["auction"]["highBidderId"] == alice["userId"]
    assert call("GET", "/me/bids", token=seller["token"])[1]["entries"] == []


# --------------------------------------------------------------------------- images

def test_image_upload_attach_and_serve(seller):
    status, up = call("POST", "/uploads", {"contentType": "image/png", "size": 1234}, token=seller["token"])
    assert status == 200 and up["method"] == "POST" and up["key"].startswith(f"uploads/{seller['userId']}/")
    body = {"title": "Pic", "startingPrice": 1, "durationSeconds": 60, "images": [up["key"]]}
    assert call("POST", "/auctions", body, token=seller["token"])[0] == 400  # not uploaded yet

    boto3.client("s3").put_object(Bucket="images", Key=up["key"], Body=b"\x89PNG")
    status, created = call("POST", "/auctions", body, token=seller["token"])
    assert status == 201 and created["auction"]["images"] == [up["key"]]
    assert call("PUT", "/me", {"avatarKey": up["key"]}, token=seller["token"])[1]["user"]["avatarKey"] == up["key"]

    event = {"rawPath": f"/images/{up['key']}", "requestContext": {"http": {"method": "GET"}}}
    r = http.handler(event, None)
    assert r["statusCode"] == 302 and up["key"] in r["headers"]["Location"]


@pytest.mark.parametrize("content_type,size", [("image/svg+xml", 10), ("image/png", 0), ("image/png", 6 * 1024 * 1024)])
def test_upload_rejects_bad_files(seller, content_type, size):
    assert call("POST", "/uploads", {"contentType": content_type, "size": size}, token=seller["token"])[0] == 400


def test_unknown_routes(aws):
    assert call("GET", "/nope")[0] == 404
    assert call("DELETE", "/auctions")[0] == 405
    assert call("OPTIONS", "/auctions")[0] == 204


# --------------------------------------------------------------------------- watching many auctions

def _second_auction(seller):
    from auction.models import new_auction_item
    item = new_auction_item({"title": "Second", "description": "", "category": "OTHER", "condition": "GOOD",
                             "quantity": 1, "images": [], "startingPrice": 500, "minIncrement": 50,
                             "startsAt": 0, "endsAt": 0}, seller)
    item.update(startsAt=item["createdAt"], endsAt=item["createdAt"] + 600_000)
    return repository.create_auction(item)


def test_watch_many_auctions_on_one_connection(auction, seller):
    second = _second_auction(seller)
    gw = FakeApiGw()
    with patch("auction.config.apigw_management_client", return_value=gw):
        ws.handler(ws_event("$connect"), None)  # anonymous: watching is public
        ws.handler(ws_event("watch", body={"action": "watch",
                                           "auctionIds": [auction["auctionId"], second["auctionId"], "missing"]}), None)
    reply = gw.sent["c1"][-1]
    assert reply["type"] == "watching"
    assert {a["auctionId"] for a in reply["auctions"]} == {auction["auctionId"], second["auctionId"]}
    assert connections.subscribers(auction["auctionId"]) == ["c1"]
    assert connections.subscribers(second["auctionId"]) == ["c1"]


def test_subscribe_after_watch_keeps_identity_and_disconnect_removes_everything(auction, seller):
    second = _second_auction(seller)
    alice = make_user("alice")
    gw = FakeApiGw()
    with patch("auction.config.apigw_management_client", return_value=gw):
        ws.handler(ws_event("$connect", token=alice["token"]), None)
        ws.handler(ws_event("watch", body={"action": "watch", "auctionIds": [auction["auctionId"]]}), None)
        ws.handler(ws_event("subscribe", body={"action": "subscribe", "auctionId": second["auctionId"]}), None)
        assert connections.get("c1")["userId"] == alice["userId"]
        ws.handler(ws_event("$disconnect"), None)
    assert connections.get("c1") is None
    assert connections.subscribers(auction["auctionId"]) == connections.subscribers(second["auctionId"]) == []


def test_broadcast_reaches_watchers_and_gone_watcher_is_fully_pruned(auction, seller):
    second = _second_auction(seller)
    connections.register("dead")
    connections.subscribe_many("dead", [auction["auctionId"], second["auctionId"]])
    connections.subscribe_many("live", [auction["auctionId"]])
    gw = FakeApiGw(gone={"dead"})
    with patch("auction.config.apigw_management_client", return_value=gw):
        connections.broadcast("https://x/dev", auction["auctionId"], {"type": "auctionUpdate"})
    assert gw.sent["live"] == [{"type": "auctionUpdate"}]
    assert connections.subscribers(second["auctionId"]) == []  # 410 pruned all of dead's rows


def test_watch_validation(aws):
    gw = FakeApiGw()
    with patch("auction.config.apigw_management_client", return_value=gw):
        ws.handler(ws_event("watch", body={"action": "watch", "auctionIds": "a1"}), None)
        ws.handler(ws_event("watch", body={"action": "watch", "auctionIds": [f"a{i}" for i in range(101)]}), None)
        ws.handler(ws_event("watch", body={"action": "watch", "auctionIds": ["bad/id"]}), None)
    assert [m["type"] for m in gw.sent["c1"]] == ["error", "error", "error"]
