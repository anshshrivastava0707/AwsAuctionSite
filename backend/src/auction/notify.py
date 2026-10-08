"""Email notifications through Amazon SES.

  outbid        to the previous leader, when someone takes the lead
  won / sold    to the winner and the seller when an auction closes
  ending soon   to everyone who saved or bid on a live auction, an hour before it ends
  removed       to the seller (with the admin's reason) and the leading bidder, when an
                admin takes a listing down

Users can switch these off on their account (emailNotifications). With no
NOTIFY_FROM configured, emails are only logged — handy locally and before SES
is set up.
"""
from __future__ import annotations

import html
import logging
import os

from . import config, repository, users
from .models import reserve_met, winner_id

log = logging.getLogger(__name__)


def _site() -> str:
    return os.environ.get("SITE_URL", "").rstrip("/")


def _money(cents) -> str:
    return f"${int(cents) / 100:,.2f}".replace(".00", "")


def _recipient(user_id: str | None) -> dict | None:
    if not user_id:
        return None
    user = users.get_user(user_id)
    if not user or not user.get("email") or user.get("emailNotifications") is False:
        return None
    return user


def send(user: dict, subject: str, lines: list[str], auction: dict) -> None:
    link = f"{_site()}/auctions/{auction['auctionId']}"
    text = "\n\n".join([f"Hi {user['displayName']},", *lines, f"View the auction: {link}",
                        "— BidBloom\n(You can turn these emails off on your account page.)"])
    body = "".join(f"<p>{html.escape(line)}</p>" for line in [f"Hi {user['displayName']},", *lines])
    body += (f'<p><a href="{html.escape(link)}" style="display:inline-block;padding:10px 18px;'
             f'background:#1a5ce6;color:#fff;border-radius:8px;text-decoration:none">View the auction</a></p>'
             f'<p style="color:#5b6475;font-size:13px">BidBloom · You can turn these emails off on your '
             f'<a href="{html.escape(_site())}/account">account page</a>.</p>')
    sender = os.environ.get("NOTIFY_FROM", "")
    if not sender:
        log.info("email (not sent, NOTIFY_FROM unset) to %s: %s", user["email"], subject)
        return
    try:
        config.ses_client().send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [user["email"]]},
            Content={"Simple": {"Subject": {"Data": subject},
                                "Body": {"Text": {"Data": text}, "Html": {"Data": body}}}},
        )
    except Exception:  # one bad address must not stop the rest
        log.exception("email to %s failed", user["email"])


def outbid(auction: dict, previous_leader: str) -> None:
    user = _recipient(previous_leader)
    if user:
        send(user, f"You've been outbid on {auction['title']}",
             [f"Someone outbid you on “{auction['title']}”. The current bid is {_money(auction['currentHigh'])}.",
              "Bid again before it ends if you still want it."], auction)


def closed(auction: dict) -> None:
    title, price = auction["title"], auction.get("currentHigh")
    winner = winner_id(auction)
    if winner and (user := _recipient(winner)):
        send(user, f"You won {title}!",
             [f"Congratulations — you won “{title}” for {_money(price)}.",
              "The seller will be in touch about payment and delivery."], auction)
    seller = _recipient(auction.get("sellerId"))
    if seller:
        if winner:
            lines = [f"“{title}” sold to {auction.get('highBidderName')} for {_money(price)}."]
            subject = f"Sold: {title}"
        elif price is not None and not reserve_met(auction):
            lines = [f"“{title}” ended at {_money(price)}, below your reserve, so it didn't sell."]
            subject = f"Reserve not met: {title}"
        else:
            lines = [f"“{title}” ended without any bids."]
            subject = f"Ended without bids: {title}"
        send(seller, subject, lines, auction)


def ending_soon(auction: dict) -> None:
    ids = dict.fromkeys(repository.savers(auction["auctionId"]) + repository.bidders(auction["auctionId"]))
    ids.pop(auction.get("sellerId"), None)
    for user_id in ids:
        user = _recipient(user_id)
        if not user:
            continue
        price = _money(auction["currentHigh"] if auction.get("currentHigh") is not None else auction["startingPrice"])
        if user_id == auction.get("highBidderId"):
            lines = [f"“{auction['title']}” ends in about an hour, and you're the highest bidder at {price}."]
        else:
            lines = [f"“{auction['title']}” ends in about an hour. The current price is {price}."]
        send(user, f"Ending soon: {auction['title']}", lines, auction)


def removed(auction: dict) -> None:
    title = auction["title"]
    seller = _recipient(auction.get("sellerId"))
    if seller:
        send(seller, f"Your listing was removed: {title}",
             [f"An admin removed “{title}” because it doesn't meet our terms.",
              f"Reason: {auction.get('removedReason') or 'not given'}",
              "Any bids on it no longer stand. Reply to this email if you think this was a mistake."], auction)
    leader = _recipient(auction.get("highBidderId"))
    if leader:
        send(leader, f"An auction you were winning was removed: {title}",
             [f"“{title}” was removed by an admin because it doesn't meet our terms, so your bid no longer stands.",
              "You won't be charged anything for it."], auction)
