"""Automatic bidding, reserve, Buy it now, soft close, browsing, saves,
Cognito tokens, notifications and thumbnails."""
import base64
import io
import itertools
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

from auction import auth, connections, notify, repository
from auction.models import EXTEND_WINDOW_MS, ValidationError, new_auction_item, parse_create_auction, public_auction
from conftest import make_user
from handlers import notify as notify_handler
from handlers import stream
from test_handlers import FakeApiGw, call


@pytest.fixture
def people(aws):
    cache: dict[str, dict] = {}

    def get(name: str) -> dict:
        if name not in cache:
            cache[name] = make_user(name)
        return cache[name]
    return get


def make_auction(seller, *, start=1000, inc=100, reserve=None, buy_now=None, ends_in=600_000,
                 category="OTHER", title="Test item", at=None):
    item = new_auction_item({
        "title": title, "description": "", "category": category, "condition": "GOOD", "quantity": 1,
        "images": [], "startingPrice": start, "minIncrement": inc, "reservePrice": reserve,
        "buyNowPrice": buy_now, "startsAt": 0, "endsAt": 0,
    }, seller, created_at_ms=at)
    item["startsAt"] = item["createdAt"]
    item["endsAt"] = item["createdAt"] + ends_in
    repository.create_auction(item)
    return item


def bid(auction, amount, user, max_amount=None, **extra):
    b = {"auctionId": auction["auctionId"], "bidId": uuid.uuid4().hex, "bidderId": user["userId"],
         "bidderName": user["displayName"], "amount": amount, **extra}
    if max_amount is not None:
        b["maxAmount"] = max_amount
    return b


def state(auction):
    return repository.get_auction(auction["auctionId"])


def history(auction):
    return sorted((int(b["amount"]), b["bidderName"], bool(b.get("auto")))
                  for b in repository.recent_bids(auction["auctionId"], limit=100))


# --------------------------------------------------------------------------- automatic bidding

def test_automatic_bid_defends_the_leader(seller, people):
    a = make_auction(seller)
    alice, bob = people("alice"), people("bob")
    r = repository.place_bid(bid(a, 1000, alice, max_amount=5000))
    assert r.accepted and r.extra["leading"] and r.extra["yourMax"] == 5000
    assert r.auction["currentHigh"] == 1000

    r = repository.place_bid(bid(a, 2000, bob))
    assert r.accepted and not r.extra["leading"]  # recorded, but immediately outbid
    s = state(a)
    assert s["highBidderId"] == alice["userId"] and int(s["currentHigh"]) == 2100 and int(s["bidCount"]) == 3
    assert history(a) == [(1000, "Alice", False), (2000, "Bob", False), (2100, "Alice", True)]


def test_ceiling_is_never_public(seller, people):
    a = make_auction(seller)
    repository.place_bid(bid(a, 1000, people("alice"), max_amount=5000))
    view = public_auction(state(a))
    assert "proxyMax" not in view and 5000 not in view.values()


def test_higher_ceiling_takes_lead_one_increment_over_old_ceiling(seller, people):
    a = make_auction(seller)
    alice, bob = people("alice"), people("bob")
    repository.place_bid(bid(a, 1000, alice, max_amount=5000))
    r = repository.place_bid(bid(a, 1100, bob, max_amount=9000))
    assert r.accepted and r.extra["leading"]
    s = state(a)
    assert s["highBidderId"] == bob["userId"] and int(s["currentHigh"]) == 5100 and int(s["proxyMax"]) == 9000
    # Alice's automatic bids went all the way to her ceiling before Bob passed it.
    assert (5000, "Alice", True) in history(a)


def test_tie_goes_to_the_earlier_bidder(seller, people):
    a = make_auction(seller)
    alice, bob = people("alice"), people("bob")
    repository.place_bid(bid(a, 1000, alice, max_amount=5000))
    r = repository.place_bid(bid(a, 5000, bob))
    assert r.accepted and not r.extra["leading"]
    s = state(a)
    assert s["highBidderId"] == alice["userId"] and int(s["currentHigh"]) == 5000


def test_leader_can_raise_ceiling_privately(seller, people):
    a = make_auction(seller)
    alice = people("alice")
    repository.place_bid(bid(a, 1000, alice, max_amount=2000))
    before = state(a)
    r = repository.place_bid(bid(a, 1100, alice, max_amount=8000))
    assert r.accepted and r.bid is None and r.extra["yourMax"] == 8000
    s = state(a)
    assert int(s["currentHigh"]) == 1000 and int(s["bidCount"]) == 1 and int(s["proxyMax"]) == 8000
    assert int(s["version"]) == int(before["version"]) + 1
    r = repository.place_bid(bid(a, 1100, alice, max_amount=8000))
    assert not r.accepted and r.reason == "ALREADY_LEADING"
    # My bids shows the ceiling to its owner only.
    _, body = call("GET", "/me/bids", token=alice["token"])
    assert body["entries"][0]["myMax"] == 8000
    assert [b["amount"] for b in body["entries"][0]["bids"]] == [1000]


@pytest.mark.parametrize("order", list(itertools.permutations(["alice", "bob", "carol"])))
def test_outcome_does_not_depend_on_bidding_order(seller, people, order):
    """Second-price rule: the top ceiling wins at one increment over the runner-up."""
    a = make_auction(seller)
    ceilings = {"alice": 3000, "bob": 7000, "carol": 4500}
    for name in order:
        s = state(a)
        if int(s["minNextBid"]) > ceilings[name]:
            continue  # already priced out; the client can't send maxAmount < amount
        repository.place_bid(bid(a, int(s["minNextBid"]), people(name), max_amount=ceilings[name]))
    s = state(a)
    assert s["highBidderId"] == people("bob")["userId"] and int(s["currentHigh"]) == 4600


def test_duplicate_submission_of_automatic_bid(seller, people):
    a = make_auction(seller)
    b = bid(a, 1000, people("alice"), max_amount=5000)
    assert repository.place_bid(b).accepted
    again = repository.place_bid(dict(b))
    assert again.accepted and again.duplicate
    assert int(state(a)["bidCount"]) == 1


def test_bid_validation_for_new_fields():
    from auction.models import parse_bid
    good = {"auctionId": "a1", "bidId": "b1", "amount": 100}
    assert parse_bid({**good, "maxAmount": 500})["maxAmount"] == 500
    assert parse_bid({**good, "buyNow": True})["buyNow"] is True
    for bad in ({**good, "maxAmount": 50}, {**good, "maxAmount": 1.5}, {**good, "buyNow": "yes"},
                {**good, "buyNow": True, "maxAmount": 500}, {**good, "bidId": "x" * 60}):
        with pytest.raises(ValidationError):
            parse_bid(bad)


# --------------------------------------------------------------------------- reserve

def test_reserve_hidden_and_reported_as_met_or_not(seller, people):
    a = make_auction(seller, reserve=3000)
    view = public_auction(state(a))
    assert view["hasReserve"] and not view["reserveMet"] and "reservePrice" not in view
    repository.place_bid(bid(a, 1000, people("alice")))
    assert not public_auction(state(a))["reserveMet"]
    repository.place_bid(bid(a, 3000, people("bob")))
    assert public_auction(state(a))["reserveMet"]


def test_ceiling_above_reserve_lifts_price_to_reserve(seller, people):
    a = make_auction(seller, reserve=3000)
    repository.place_bid(bid(a, 1000, people("alice"), max_amount=4000))
    s = state(a)
    assert int(s["currentHigh"]) == 3000 and public_auction(s)["reserveMet"]


def test_reserve_not_met_means_no_winner(seller, people):
    from auction.models import winner_id
    a = make_auction(seller, reserve=3000)
    repository.place_bid(bid(a, 1000, people("alice")))
    assert repository.close_auction(a["auctionId"], at_ms=a["endsAt"])
    assert winner_id(state(a)) is None


def test_listing_price_rules():
    base = {"title": "x", "startingPrice": 1000, "durationSeconds": 3600}
    assert parse_create_auction({**base, "reservePrice": 2000, "buyNowPrice": 5000}, "u")["buyNowPrice"] == 5000
    for bad in ({"reservePrice": 1000}, {"buyNowPrice": 900}, {"reservePrice": 5000, "buyNowPrice": 4000}):
        with pytest.raises(ValidationError):
            parse_create_auction({**base, **bad}, "u")


# --------------------------------------------------------------------------- Buy it now

def test_buy_now_closes_the_auction(seller, people):
    a = make_auction(seller, buy_now=9000)
    assert public_auction(state(a))["buyNowPrice"] == 9000
    r = repository.place_bid(bid(a, 9000, people("alice"), buyNow=True))
    assert r.accepted and r.auction["phase"] == "ENDED" and r.auction["soldVia"] == "BUY_NOW"
    s = state(a)
    assert s["status"] == "CLOSED" and s["highBidderId"] == people("alice")["userId"] and "openListing" not in s
    r = repository.place_bid(bid(a, 9100, people("bob")))
    assert not r.accepted and r.reason == "AUCTION_CLOSED"


def test_buy_now_disappears_after_first_bid_and_checks_price(seller, people):
    a = make_auction(seller, buy_now=9000)
    r = repository.place_bid(bid(a, 8000, people("alice"), buyNow=True))
    assert not r.accepted and r.reason == "TERMS_CHANGED"
    repository.place_bid(bid(a, 1000, people("bob")))
    assert public_auction(state(a))["buyNowPrice"] is None
    r = repository.place_bid(bid(a, 9000, people("alice"), buyNow=True))
    assert not r.accepted and r.reason == "BUY_NOW_UNAVAILABLE"


# --------------------------------------------------------------------------- soft close

def test_late_bid_extends_the_end(seller, people):
    a = make_auction(seller)
    late = a["endsAt"] - 30_000
    r = repository.place_bid(bid(a, 1000, people("alice")), at_ms=late)
    assert r.accepted and r.extra["extendedTo"] == late + EXTEND_WINDOW_MS
    assert int(state(a)["endsAt"]) == late + EXTEND_WINDOW_MS
    # The old end has passed, but the auction is still live.
    assert repository.place_bid(bid(a, 1100, people("bob")), at_ms=a["endsAt"] + 5_000).accepted


def test_early_bid_does_not_extend(seller, people):
    a = make_auction(seller)
    r = repository.place_bid(bid(a, 1000, people("alice")), at_ms=a["endsAt"] - EXTEND_WINDOW_MS - 1)
    assert r.accepted and "extendedTo" not in r.extra and int(state(a)["endsAt"]) == a["endsAt"]


# --------------------------------------------------------------------------- browsing

def test_sorts_search_and_paging(seller, people):
    now = int(time.time() * 1000)
    a = make_auction(seller, title="Vintage film camera", category="ELECTRONICS", ends_in=3_600_000, at=now - 3000)
    b = make_auction(seller, title="Modernist painting", category="ART", start=24000, ends_in=600_000, at=now - 2000)
    c = make_auction(seller, title="Archive sneakers", category="FASHION", start=500, ends_in=7_200_000, at=now - 1000)
    repository.place_bid(bid(c, 12500, people("alice")))
    ids = lambda items: [i["auctionId"] for i in items]  # noqa: E731

    assert ids(repository.list_auctions(sort="newest")[0]) == [c["auctionId"], b["auctionId"], a["auctionId"]]
    assert ids(repository.list_auctions(sort="ending")[0]) == [b["auctionId"], a["auctionId"], c["auctionId"]]
    assert ids(repository.list_auctions(sort="price_low")[0]) == [a["auctionId"], c["auctionId"], b["auctionId"]]
    assert ids(repository.list_auctions(sort="price_high")[0]) == [b["auctionId"], c["auctionId"], a["auctionId"]]
    assert ids(repository.list_auctions("ART", sort="ending")[0]) == [b["auctionId"]]
    assert ids(repository.list_auctions(query="FILM camera")[0]) == [a["auctionId"]]
    assert ids(repository.list_auctions(query="seller")[0]) != []  # seller name is searchable
    assert repository.list_auctions(query="tractor")[0] == []

    # Paging walks every item exactly once, for both cursor kinds.
    for sort in ("newest", "ending", "price_low"):
        seen, cursor = [], None
        while True:
            page, cursor = repository.list_auctions(limit=2, sort=sort, cursor=cursor)
            seen += ids(page)
            if not cursor:
                break
        assert sorted(seen) == sorted([a["auctionId"], b["auctionId"], c["auctionId"]]), sort

    # Closed auctions leave the "ending" index but stay in "newest".
    repository.close_auction(b["auctionId"], at_ms=b["endsAt"])
    assert b["auctionId"] not in ids(repository.list_auctions(sort="ending")[0])
    assert b["auctionId"] in ids(repository.list_auctions(sort="newest")[0])

    status, body = call("GET", "/auctions", query={"sort": "ending", "limit": "1"})
    assert status == 200 and len(body["auctions"]) == 1 and body["nextCursor"]
    assert call("GET", "/auctions", query={"sort": "cheapest"})[0] == 400


def test_edit_keeps_search_in_step(seller):
    a = make_auction(seller, title="Old name")
    repository.update_auction(a["auctionId"], seller["userId"], {"title": "Shiny new lamp"})
    assert [i["auctionId"] for i in repository.list_auctions(query="lamp")[0]] == [a["auctionId"]]


# --------------------------------------------------------------------------- saves

def test_save_and_unsave(seller, people):
    a = make_auction(seller)
    alice = people("alice")
    assert call("PUT", f"/me/saved/{a['auctionId']}", token=alice["token"])[0] == 200
    assert call("PUT", f"/me/saved/{a['auctionId']}", token=alice["token"])[0] == 200  # idempotent
    assert public_auction(state(a))["watchCount"] == 1
    _, body = call("GET", "/me/saved", token=alice["token"])
    assert [x["auctionId"] for x in body["auctions"]] == [a["auctionId"]]
    assert repository.savers(a["auctionId"]) == [alice["userId"]]
    call("DELETE", f"/me/saved/{a['auctionId']}", token=alice["token"])
    call("DELETE", f"/me/saved/{a['auctionId']}", token=alice["token"])
    assert public_auction(state(a))["watchCount"] == 0
    assert call("PUT", "/me/saved/nope", token=alice["token"])[0] == 404
    assert call("GET", "/me/saved")[0] == 401


def test_saves_do_not_disturb_bids_or_broadcast(seller, people):
    a = make_auction(seller)
    before = state(a)
    repository.set_saved(people("alice")["userId"], a["auctionId"], True)
    after = state(a)
    assert after["version"] == before["version"]
    record = {"eventName": "MODIFY", "dynamodb": {
        "OldImage": _ddb(before), "NewImage": _ddb(after)}}
    api = FakeApiGw()
    with _patched_apigw(api):
        stream.handler({"Records": [record]}, None)
    assert api.sent == {}


def _ddb(item):
    from boto3.dynamodb.types import TypeSerializer
    ser = TypeSerializer()
    return {k: ser.serialize(v) for k, v in item.items()}


class _patched_apigw:
    def __init__(self, api):
        from unittest.mock import patch
        self.patches = [patch("auction.config.apigw_management_client", return_value=api),
                        patch.dict("os.environ", {"WS_ENDPOINT": "https://x/dev"})]

    def __enter__(self):
        for p in self.patches:
            p.__enter__()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.__exit__(*exc)


def test_stream_sends_every_bid_row_of_a_commit(seller, people, monkeypatch):
    a = make_auction(seller)
    repository.place_bid(bid(a, 1000, people("alice"), max_amount=5000))
    before = state(a)
    connections.register("c1")
    connections.subscribe("c1", a["auctionId"])
    repository.place_bid(bid(a, 2000, people("bob")))
    after = state(a)
    api = FakeApiGw()
    with _patched_apigw(api):
        stream.handler({"Records": [{"eventName": "MODIFY", "dynamodb": {
            "OldImage": _ddb(before), "NewImage": _ddb(after)}}]}, None)
    payload = api.sent["c1"][0]
    assert [(b["amount"], b["bidderName"], b["auto"]) for b in payload["bids"]] == [
        (2000, "Bob", False), (2100, "Alice", True)]


# --------------------------------------------------------------------------- Cognito

@pytest.fixture
def cognito(monkeypatch):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.public_key().public_numbers()
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    as_bytes = lambda i: i.to_bytes((i.bit_length() + 7) // 8, "big")  # noqa: E731
    jwks = {"keys": [{"kid": "k1", "kty": "RSA", "alg": "RS256", "n": b64(as_bytes(nums.n)), "e": b64(as_bytes(nums.e))}]}
    monkeypatch.setenv("AUTH_MODE", "cognito")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_TestPool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "client123")
    monkeypatch.setattr(auth, "_fetch_jwks", lambda issuer: jwks)
    monkeypatch.setattr(auth, "_jwks_cache", {"keys": {}, "fetched": 0.0})
    issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool"

    def token(kid="k1", sign_with=key, **overrides):
        now = int(time.time())
        claims = {"sub": "abc-123", "email": "Alice@Example.com", "email_verified": True, "iss": issuer,
                  "aud": "client123", "token_use": "id", "iat": now, "exp": now + 3600, **overrides}
        head = b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
        body = b64(json.dumps(claims).encode())
        sig = sign_with.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{body}.{b64(sig)}"
    token.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return token


def test_cognito_token_accepted(cognito):
    assert auth.identify(cognito()) == auth.Identity(user_id="abc-123", email="alice@example.com")


@pytest.mark.parametrize("bad", [
    {"aud": "someone-else"}, {"iss": "https://evil.example"}, {"token_use": "access"},
    {"exp": 1}, {"email_verified": False}, {"kid": "unknown"}, {"forged": True},
])
def test_cognito_token_rejected(cognito, bad):
    if bad.get("forged"):
        tok = cognito(sign_with=cognito.other_key)
    elif "kid" in bad:
        tok = cognito(kid=bad["kid"])
    else:
        tok = cognito(**bad)
    assert auth.identify(tok) is None


def test_cognito_tampered_payload_rejected(cognito):
    head, _, sig = cognito().split(".")
    body = base64.urlsafe_b64encode(json.dumps({"sub": "admin", "email": "x@y.z"}).encode()).rstrip(b"=").decode()
    assert auth.identify(f"{head}.{body}.{sig}") is None
    assert auth.identify("not-a-jwt") is None


def test_dev_login_disabled_in_cognito_mode(cognito, aws):
    assert call("POST", "/auth/dev-login", {"email": "a@b.co"})[0] == 404


# --------------------------------------------------------------------------- notifications

@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "send", lambda user, subject, lines, auction: out.append((user["email"], subject)))
    return out


def _stream_event(before, after):
    return {"Records": [{"eventName": "MODIFY", "dynamodb": {"OldImage": _ddb(before), "NewImage": _ddb(after)}}]}


def test_outbid_email_goes_to_previous_leader(seller, people, sent):
    a = make_auction(seller)
    repository.place_bid(bid(a, 1000, people("alice")))
    before = state(a)
    repository.place_bid(bid(a, 2000, people("bob")))
    notify_handler.handler(_stream_event(before, state(a)), None)
    assert sent == [("alice@example.com", "You've been outbid on Test item")]


def test_no_outbid_email_when_automatic_bid_defends(seller, people, sent):
    a = make_auction(seller)
    repository.place_bid(bid(a, 1000, people("alice"), max_amount=5000))
    before = state(a)
    repository.place_bid(bid(a, 2000, people("bob")))
    notify_handler.handler(_stream_event(before, state(a)), None)
    assert sent == []


def test_close_emails_winner_and_seller(seller, people, sent):
    a = make_auction(seller)
    repository.place_bid(bid(a, 1000, people("alice")))
    before = state(a)
    repository.close_auction(a["auctionId"], at_ms=a["endsAt"])
    notify_handler.handler(_stream_event(before, state(a)), None)
    assert sent == [("alice@example.com", "You won Test item!"), ("seller@example.com", "Sold: Test item")]


def test_close_below_reserve_emails_only_seller(seller, people, sent):
    a = make_auction(seller, reserve=5000)
    repository.place_bid(bid(a, 1000, people("alice")))
    before = state(a)
    repository.close_auction(a["auctionId"], at_ms=a["endsAt"])
    notify_handler.handler(_stream_event(before, state(a)), None)
    assert sent == [("seller@example.com", "Reserve not met: Test item")]


def test_ending_soon_reaches_savers_and_bidders_once(seller, people, sent):
    a = make_auction(seller)
    alice, bob, carol = people("alice"), people("bob"), people("carol")
    repository.place_bid(bid(a, 1000, alice))
    repository.set_saved(alice["userId"], a["auctionId"], True)
    repository.set_saved(bob["userId"], a["auctionId"], True)
    from auction import users
    users.update_profile(carol["userId"], {"emailNotifications": False})
    repository.set_saved(carol["userId"], a["auctionId"], True)
    notify_handler.handler({"kind": "endingSoon", "auctionId": a["auctionId"]}, None)
    assert sorted(e for e, _ in sent) == ["alice@example.com", "bob@example.com"]


def test_email_preference_on_profile(people):
    alice = people("alice")
    status, body = call("PUT", "/me", {"emailNotifications": False}, token=alice["token"])
    assert status == 200 and body["user"]["emailNotifications"] is False
    assert call("PUT", "/me", {"emailNotifications": "no"}, token=alice["token"])[0] == 400


# --------------------------------------------------------------------------- thumbnails

def test_thumbnail_is_small_webp_and_upright():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "thumbs"))
    from PIL import Image
    import app as thumbs

    im = Image.new("RGB", (3000, 2000), (200, 30, 30))
    exif = im.getexif()
    exif[0x0112] = 6  # "rotate 90° when displayed", as phones write it
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    out = Image.open(io.BytesIO(thumbs.make_thumb(buf.getvalue())))
    assert out.format == "WEBP" and max(out.size) == thumbs.MAX_SIDE
    assert out.size[1] > out.size[0]  # rotated to portrait
    assert thumbs.thumb_key("uploads/u_1/abc.jpg") == "thumbs/u_1/abc.webp"


def test_image_route_serves_thumbnail_key(aws):
    key = "uploads/u_1/" + "a" * 32 + ".jpg"
    r_full = call_raw(f"/images/{key}")
    r_thumb = call_raw(f"/images/{key}", {"size": "thumb"})
    assert "/uploads/u_1/" in r_full["headers"]["Location"]
    assert "/thumbs/u_1/" + "a" * 32 + ".webp" in r_thumb["headers"]["Location"]


def call_raw(path, query=None):
    from handlers import http
    return http.handler({"rawPath": path, "requestContext": {"http": {"method": "GET", "path": path}},
                         "headers": {}, "queryStringParameters": query, "body": None}, None)
