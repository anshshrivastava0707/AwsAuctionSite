"""Email notifications through Amazon SES.

  outbid        to the previous leader, when someone takes the lead
  sale          when an auction closes with a winner: ONE confirmation to the buyer with
                the seller CC'd — item, final price, reference, and both parties'
                contact details — so each side holds the same record of the deal.
                Sent even to people who turned notifications off (it's a record, not an alert).
  no sale       to the seller: nobody bid, or the reserve wasn't met
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
from datetime import datetime, timezone

from . import config, repository, users
from .models import reserve_met, winner_id

log = logging.getLogger(__name__)


def _site() -> str:
    return os.environ.get("SITE_URL", "").rstrip("/")


def _money(cents) -> str:
    return f"${int(cents) / 100:,.2f}".replace(".00", "")


CONDITIONS = {"NEW": "New", "LIKE_NEW": "Like new", "GOOD": "Good", "FAIR": "Fair",
              "FOR_PARTS": "For parts / not working"}
_BUTTON = ('display:inline-block;padding:10px 18px;background:#1a5ce6;color:#fff;border-radius:8px;'
           'text-decoration:none')
_FOOTNOTE = "color:#5b6475;font-size:13px"


def _when(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%d %b %Y, %H:%M UTC").lstrip("0")


def reference(auction: dict) -> str:
    """Short, quotable id for a sale ("BB-1A2B3C4D")."""
    return "BB-" + auction["auctionId"][:8].upper()


def _account(user_id: str | None) -> dict | None:
    user = users.get_user(user_id) if user_id else None
    return user if user and user.get("email") else None


def _recipient(user_id: str | None) -> dict | None:
    """An account that wants notification emails."""
    user = _account(user_id)
    return user if user and user.get("emailNotifications") is not False else None


def _deliver(to: list[str], subject: str, text: str, body_html: str, cc: list[str] | None = None) -> None:
    sender = os.environ.get("NOTIFY_FROM", "")
    if not sender:
        log.info("email (not sent, NOTIFY_FROM unset) to %s%s: %s", ", ".join(to),
                 f" cc {', '.join(cc)}" if cc else "", subject)
        return
    destination: dict = {"ToAddresses": to}
    if cc:
        destination["CcAddresses"] = cc
    try:
        config.ses_client().send_email(
            FromEmailAddress=sender,
            Destination=destination,
            Content={"Simple": {"Subject": {"Data": subject},
                                "Body": {"Text": {"Data": text}, "Html": {"Data": body_html}}}},
        )
    except Exception:  # one bad address must not stop the rest
        log.exception("email to %s failed", ", ".join(to + (cc or [])))


def send(user: dict, subject: str, lines: list[str], auction: dict, *, link: str | None = None,
         link_label: str = "View the auction") -> None:
    link = link or f"{_site()}/auctions/{auction['auctionId']}"
    text = "\n\n".join([f"Hi {user['displayName']},", *lines, f"{link_label}: {link}",
                        "— BidBloom\n(You can turn these emails off on your account page.)"])
    body = "".join(f"<p>{html.escape(line)}</p>" for line in [f"Hi {user['displayName']},", *lines])
    body += (f'<p><a href="{html.escape(link)}" style="{_BUTTON}">{html.escape(link_label)}</a></p>'
             f'<p style="{_FOOTNOTE}">BidBloom · You can turn these emails off on your '
             f'<a href="{html.escape(_site())}/account">account page</a>.</p>')
    _deliver([user["email"]], subject, text, body)


def outbid(auction: dict, previous_leader: str) -> None:
    user = _recipient(previous_leader)
    if user:
        send(user, f"You've been outbid on {auction['title']}",
             [f"Someone outbid you on “{auction['title']}”. The current bid is {_money(auction['currentHigh'])}.",
              "Bid again before it ends if you still want it."], auction)


def closed(auction: dict) -> None:
    title, price = auction["title"], auction.get("currentHigh")
    winner = winner_id(auction)
    if winner:
        buyer, seller = _account(winner), _account(auction.get("sellerId"))
        if buyer and seller:
            sale_confirmation(auction, buyer, seller)
        return
    seller = _recipient(auction.get("sellerId"))
    if not seller:
        return
    relist = f"{_site()}/sell"
    if price is not None and not reserve_met(auction):
        send(seller, f"Reserve not met: {title}",
             [f"“{title}” has ended. The highest bid was {_money(price)}, which is below your reserve price, "
              "so the item didn't sell and nobody has to pay anything.",
              "You can list it again, perhaps with a lower reserve."],
             auction, link=relist, link_label="List it again")
    else:
        send(seller, f"No bids on {title}",
             [f"“{title}” has ended, and unfortunately nobody placed a bid this time.",
              "Listings with clear photos, a detailed description and a lower starting price usually draw "
              "more bidders. You can list it again whenever you like."],
             auction, link=relist, link_label="List it again")


def sale_confirmation(auction: dict, buyer: dict, seller: dict) -> None:
    """One email, buyer in To and seller in CC, so both hold the identical record."""
    price = int(auction["currentHigh"])
    ref = reference(auction)
    bids = int(auction.get("bidCount") or 0)
    how = ("Bought with Buy it now" if auction.get("soldVia") == "BUY_NOW"
           else f"Highest bid, after {bids} bid{'s' if bids != 1 else ''}")
    quantity = int(auction.get("quantity") or 1)
    description = (auction.get("description") or "").strip()
    if len(description) > 400:
        description = description[:400].rstrip() + "…"
    rows = [
        ("Reference", ref),
        ("Item", auction["title"] + (f" (lot of {quantity}, sold together)" if quantity > 1 else "")),
        ("Condition", CONDITIONS.get(auction.get("condition", ""), auction.get("condition", ""))),
        *([("Description", description)] if description else []),
        ("Final price", _money(price)),
        ("How it was won", how),
        ("Auction ended", _when(auction["endsAt"])),
        ("Seller", f"{seller['displayName']} <{seller['email']}>"),
        ("Buyer", f"{buyer['displayName']} <{buyer['email']}>"),
    ]
    link = f"{_site()}/auctions/{auction['auctionId']}"
    intro = (f"{buyer['displayName']} won “{auction['title']}” from {seller['displayName']} for {_money(price)}. "
             "This email goes to both of you, so you each have the same record of what was agreed.")
    steps = ["Buyer and seller: reply to each other directly (you're both on this email) to arrange payment "
             "and delivery.",
             f"Quote the reference {ref} if you contact BidBloom about this sale.",
             "Keep this email as your record of the item and the agreed price."]

    width = max(len(k) for k, _ in rows)
    text = "\n".join([
        f"Sale confirmation {ref}", "", intro, "",
        *[f"{k.ljust(width)}  {v}" for k, v in rows], "",
        "Next steps:", *[f"- {s}" for s in steps], "",
        f"View the auction: {link}", "", "— BidBloom",
    ])
    table = "".join(
        f'<tr><td style="padding:8px 12px;color:#5b6475;white-space:nowrap;vertical-align:top">{html.escape(k)}</td>'
        f'<td style="padding:8px 12px;{"font-weight:700;font-size:18px" if k == "Final price" else ""}">'
        f"{html.escape(v)}</td></tr>"
        for k, v in rows
    )
    body = (
        f'<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:600px;color:#0e1525">'
        f'<h2 style="margin:0 0 4px">Sale confirmed</h2>'
        f'<p style="margin:0 0 16px;color:#5b6475">Reference {html.escape(ref)}</p>'
        f"<p>{html.escape(intro)}</p>"
        f'<table style="border-collapse:collapse;width:100%;border:1px solid #e3e6ec;border-radius:8px;'
        f'margin:16px 0">{table}</table>'
        f'<p style="margin-bottom:4px"><strong>Next steps</strong></p>'
        f'<ul style="margin-top:4px">{"".join(f"<li>{html.escape(s)}</li>" for s in steps)}</ul>'
        f'<p><a href="{html.escape(link)}" style="{_BUTTON}">View the auction</a></p>'
        f'<p style="{_FOOTNOTE}">BidBloom · This confirmation is sent to both parties of every completed sale.</p>'
        f"</div>"
    )
    _deliver([buyer["email"]], f"Sale confirmed: {auction['title']} for {_money(price)} ({ref})",
             text, body, cc=[seller["email"]])


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
