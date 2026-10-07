import threading
import uuid

import pytest

from auction import repository, users
from auction.errors import Conflict, Forbidden
from auction.models import ValidationError, parse_bid
from conftest import make_user


@pytest.fixture
def people(aws):
    """people("alice") -> an approved buyer (created on first use)."""
    cache: dict[str, dict] = {}

    def get(name: str, **kw) -> dict:
        if name not in cache:
            cache[name] = make_user(name, **kw)
        return cache[name]
    return get


def bid(auction, amount, user, bid_id=None, **extra):
    return {
        "auctionId": auction["auctionId"],
        "bidId": bid_id or uuid.uuid4().hex,
        "bidderId": user["userId"],
        "bidderName": user["displayName"],
        "amount": amount,
        **extra,
    }


def test_first_bid_must_meet_starting_price(auction, people):
    r = repository.place_bid(bid(auction, 999, people("alice")))
    assert not r.accepted and r.reason == "BID_TOO_LOW"
    r = repository.place_bid(bid(auction, 1000, people("alice")))
    assert r.accepted
    assert r.auction["currentHigh"] == 1000
    assert r.auction["minNextBid"] == 1100
    assert r.auction["version"] == 2


def test_lower_equal_and_below_increment_bids_rejected(auction, people):
    alice, bob = people("alice"), people("bob")
    assert repository.place_bid(bid(auction, 2000, alice)).accepted
    for amount in (1500, 2000, 2099):
        r = repository.place_bid(bid(auction, amount, bob))
        assert not r.accepted and r.reason == "BID_TOO_LOW", amount
        assert r.auction["currentHigh"] == 2000  # rejected bidder learns the real state
    state = repository.get_auction(auction["auctionId"])
    assert state["highBidderId"] == alice["userId"] and int(state["version"]) == 2


def test_stale_bid_rejected_after_being_outbid(auction, people):
    # Bob saw minNextBid=1000 and bids 1200, but Carol's 1500 committed first.
    assert repository.place_bid(bid(auction, 1500, people("carol"))).accepted
    r = repository.place_bid(bid(auction, 1200, people("bob")))
    assert not r.accepted and r.reason == "BID_TOO_LOW"
    assert repository.get_auction(auction["auctionId"])["highBidderId"] == people("carol")["userId"]


def test_duplicate_submission_is_idempotent(auction, people):
    b = bid(auction, 1000, people("alice"))
    first = repository.place_bid(b)
    again = repository.place_bid(dict(b))  # e.g. client re-sends after reconnect
    assert first.accepted and again.accepted and again.duplicate
    state = repository.get_auction(auction["auctionId"])
    assert int(state["bidCount"]) == 1 and int(state["version"]) == 2


def test_reused_bid_id_with_different_amount_rejected(auction, people):
    b = bid(auction, 1000, people("alice"))
    assert repository.place_bid(b).accepted
    r = repository.place_bid({**b, "amount": 5000})
    assert not r.accepted and r.reason == "DUPLICATE_ID"
    assert int(repository.get_auction(auction["auctionId"])["currentHigh"]) == 1000


def test_bid_after_end_rejected_even_before_close_job(auction, people):
    r = repository.place_bid(bid(auction, 5000, people("alice")), at_ms=auction["endsAt"])
    assert not r.accepted and r.reason == "AUCTION_CLOSED"
    assert r.auction["isOpen"] is False and r.auction["phase"] == "ENDED"


def test_close_is_conditional_and_once_only(auction, people):
    alice = people("alice")
    assert repository.place_bid(bid(auction, 1000, alice)).accepted
    assert not repository.close_auction(auction["auctionId"], at_ms=auction["endsAt"] - 1)  # too early
    assert repository.close_auction(auction["auctionId"], at_ms=auction["endsAt"])
    assert not repository.close_auction(auction["auctionId"], at_ms=auction["endsAt"] + 5)  # already closed
    r = repository.place_bid(bid(auction, 9000, people("bob")), at_ms=auction["endsAt"] - 10)
    assert not r.accepted and r.reason == "AUCTION_CLOSED"
    state = repository.get_auction(auction["auctionId"])
    assert state["status"] == "CLOSED" and state["highBidderId"] == alice["userId"]


def test_unknown_auction(people):
    r = repository.place_bid({"auctionId": "nope", "bidId": "b1", "bidderId": people("a")["userId"],
                              "bidderName": "A", "amount": 100})
    assert not r.accepted and r.reason == "NOT_FOUND"


# --------------------------------------------------------------------------- approval / roles

@pytest.mark.parametrize("status", ["PENDING", "REJECTED", "NONE"])
def test_unapproved_buyer_cannot_bid(auction, people, status):
    r = repository.place_bid(bid(auction, 1000, people("newbie", buyer=status)))
    assert not r.accepted and r.reason == "NOT_APPROVED"
    assert int(repository.get_auction(auction["auctionId"])["bidCount"]) == 0


def test_revoked_approval_takes_effect_on_next_bid(auction, people):
    alice = people("alice")
    assert repository.place_bid(bid(auction, 1000, alice)).accepted
    users.set_approval(alice["userId"], {"buyerStatus": "REJECTED"})
    r = repository.place_bid(bid(auction, 2000, alice))
    assert not r.accepted and r.reason == "NOT_APPROVED"


def test_seller_cannot_bid_on_own_auction(auction, seller):
    r = repository.place_bid(bid(auction, 1000, seller))
    assert not r.accepted and r.reason == "OWN_AUCTION"


def test_scheduled_auction_rejects_bids_until_start(auction, people):
    starts = auction["createdAt"] + 60_000
    repository._auctions().update_item(Key={"auctionId": auction["auctionId"]},
                                       UpdateExpression="SET startsAt = :s", ExpressionAttributeValues={":s": starts})
    r = repository.place_bid(bid(auction, 1000, people("alice")), at_ms=starts - 1)
    assert not r.accepted and r.reason == "NOT_STARTED" and r.auction["phase"] == "SCHEDULED"
    assert repository.place_bid(bid(auction, 1000, people("alice")), at_ms=starts).accepted


# --------------------------------------------------------------------------- edit / cancel

def test_seller_can_edit_before_first_bid(auction, seller):
    new, changes = repository.update_auction(auction["auctionId"], seller["userId"],
                                             {"title": "Better title", "startingPrice": 5000})
    assert new["title"] == "Better title" and int(new["minNextBid"]) == 5000
    assert int(new["termsVersion"]) == 2 and int(new["version"]) == 2
    assert set(changes) == {"title", "startingPrice", "minNextBid"}


def test_edit_rejected_after_first_bid_and_for_non_seller(auction, seller, people):
    with pytest.raises(Forbidden):
        repository.update_auction(auction["auctionId"], people("mallory")["userId"], {"title": "Mine now"})
    assert repository.place_bid(bid(auction, 1000, people("alice"))).accepted
    with pytest.raises(Conflict):
        repository.update_auction(auction["auctionId"], seller["userId"], {"title": "Too late"})


def test_bid_pinned_to_old_terms_is_rejected_after_edit(auction, seller, people):
    repository.update_auction(auction["auctionId"], seller["userId"], {"description": "Actually it's broken"})
    r = repository.place_bid(bid(auction, 1000, people("alice"), termsVersion=1))
    assert not r.accepted and r.reason == "TERMS_CHANGED"
    assert repository.place_bid(bid(auction, 1000, people("alice"), termsVersion=2)).accepted


def test_edit_validates_schedule(auction, seller):
    with pytest.raises(ValidationError):
        repository.update_auction(auction["auctionId"], seller["userId"], {"endsAt": auction["createdAt"]})
    with pytest.raises(ValidationError):
        repository.update_auction(auction["auctionId"], seller["userId"], {"sellerId": "someone-else"})


def test_cancel_before_bids_then_bids_rejected(auction, seller, people):
    item = repository.cancel_auction(auction["auctionId"], seller["userId"])
    assert item["status"] == "CANCELLED"
    r = repository.place_bid(bid(auction, 1000, people("alice")))
    assert not r.accepted and r.reason == "AUCTION_CLOSED" and r.auction["phase"] == "CANCELLED"
    with pytest.raises(Conflict):
        repository.cancel_auction(auction["auctionId"], seller["userId"])


def test_cancel_after_bid_only_by_admin(auction, seller, people):
    assert repository.place_bid(bid(auction, 1000, people("alice"))).accepted
    with pytest.raises(Conflict):
        repository.cancel_auction(auction["auctionId"], seller["userId"])
    assert repository.cancel_auction(auction["auctionId"], "admin-id", is_admin=True)["status"] == "CANCELLED"


# --------------------------------------------------------------------------- concurrency

@pytest.fixture
def atomic_moto():
    """moto's in-process DynamoDB checks conditions and applies writes as separate
    Python steps, so under real threads it can lose updates that real DynamoDB
    never would (DynamoDB evaluates + applies each request atomically — that is
    the guarantee place_bid relies on). Emulate that service-side atomicity by
    serialising moto's backend operations. The end-to-end proof against the real
    service is scripts/concurrency_test.py."""
    from moto.dynamodb.models import DynamoDBBackend

    lock = threading.RLock()
    names = ["transact_write_items", "update_item", "put_item", "get_item", "delete_item", "query"]
    originals = {n: getattr(DynamoDBBackend, n) for n in names}

    def wrap(fn):
        def locked(*a, **kw):
            with lock:
                return fn(*a, **kw)
        return locked

    for n, fn in originals.items():
        setattr(DynamoDBBackend, n, wrap(fn))
    yield
    for n, fn in originals.items():
        setattr(DynamoDBBackend, n, fn)


@pytest.mark.parametrize("run", range(5))
def test_concurrent_bids_highest_wins_and_history_is_monotonic(auction, people, atomic_moto, run):
    """Fire many bids at once from many threads. Whatever the interleaving:
    - the final high bid is the maximum amount that was accepted,
    - the highest amount submitted is always accepted (nothing can beat it),
    - accepted bids form a strictly increasing sequence (no lost/overwritten updates),
    - version == 1 + number of accepted bids (every commit is counted exactly once)."""
    amounts = list(range(1000, 1000 + 40 * 100, 100))  # 40 valid, distinct amounts
    import random
    random.shuffle(amounts)
    bidders = [people(f"user{i}") for i in range(len(amounts))]
    barrier = threading.Barrier(len(amounts))
    results = {}

    def worker(i, amount):
        b = bid(auction, amount, bidders[i])
        barrier.wait()
        results[amount] = repository.place_bid(b, sleep=lambda s: None)

    threads = [threading.Thread(target=worker, args=(i, a)) for i, a in enumerate(amounts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = sorted(a for a, r in results.items() if r.accepted)
    rejected = [r for r in results.values() if not r.accepted]
    assert all(r.reason in ("BID_TOO_LOW", "BUSY", "STALE") for r in rejected)

    state = repository.get_auction(auction["auctionId"])
    assert max(amounts) in accepted
    assert int(state["currentHigh"]) == max(amounts)
    assert int(state["version"]) == 1 + len(accepted)
    assert int(state["bidCount"]) == len(accepted)

    history = [int(b["amount"]) for b in repository.recent_bids(auction["auctionId"], limit=100)]
    assert sorted(history) == accepted


def test_bid_validation():
    good = {"auctionId": "a1", "bidId": "b1", "amount": 100}
    assert parse_bid(good)["amount"] == 100
    assert "bidderId" not in parse_bid({**good, "bidderId": "spoofed"})  # identity never comes from the client
    for bad in ({**good, "amount": 10.5}, {**good, "amount": "100"}, {**good, "amount": True},
                {**good, "amount": 0}, {**good, "bidId": "x/y"}, {**good, "termsVersion": "1"}):
        with pytest.raises(ValidationError):
            parse_bid(bad)
