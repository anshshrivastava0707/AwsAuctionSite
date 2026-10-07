"""HTTP API handler. API Gateway sends every path here (`ANY /{proxy+}`) and this
module routes by method + path, so adding an endpoint never touches the template.

Public (no login):
  GET  /auctions?category=X        newest first
  GET  /auctions/{id}              snapshot (strongly consistent) — used by page loads
  GET  /users/{id}                 public profile
  GET  /users/{id}/auctions        a seller's listings
  GET  /images/{key}               redirect to the stored image
  POST /auth/dev-login             AUTH_MODE=dev only

Logged in:
  GET  /me    POST /me (create account)    PUT /me (edit profile)
  POST /me/seller-request
  GET  /me/bids                    bidding history, grouped by auction
  GET  /me/auctions                my listings, including cancelled
  POST /uploads                    signed upload for one image
  POST /auctions                   approved sellers only
  PATCH /auctions/{id}             seller, before the first bid
  POST /auctions/{id}/cancel       seller before the first bid; admins any time

Admin:
  GET  /admin/users?filter=pending|all
  POST /admin/users/{id}           {buyerStatus?, sellerStatus?}
"""
from __future__ import annotations

import base64
import json
import logging
import re

from auction import auth, repository, schedules, storage, users
from auction.errors import Conflict, Forbidden, NotFound
from auction.models import (
    APPROVED, CATEGORIES, IMAGE_KEY_RE, ValidationError, new_auction_item, parse_approval, parse_auction_id,
    parse_create_auction, parse_id, parse_profile_update, parse_registration, private_user, public_auction,
    public_bid, public_user,
)

log = logging.getLogger()
log.setLevel(logging.INFO)


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _resp(status: int, body=None, headers: dict | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store", **(headers or {})},
        "body": json.dumps(body if body is not None else {}),
    }


class Request:
    def __init__(self, event: dict):
        http = (event.get("requestContext") or {}).get("http") or {}
        self.method = (http.get("method") or event.get("httpMethod") or "GET").upper()
        self.path = (event.get("rawPath") or http.get("path") or "/").rstrip("/") or "/"
        self.query = event.get("queryStringParameters") or {}
        self.headers = event.get("headers") or {}
        raw = event.get("body")
        if raw and event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode()
        self._raw = raw
        self._identity: auth.Identity | None = None
        self._identity_loaded = False

    def json(self) -> dict:
        try:
            return json.loads(self._raw or "{}")
        except json.JSONDecodeError:
            raise HttpError(400, "Body must be valid JSON.")

    @property
    def identity(self) -> auth.Identity | None:
        if not self._identity_loaded:
            self._identity = auth.identify(auth.bearer_token(self.headers))
            self._identity_loaded = True
        return self._identity

    def require_identity(self) -> auth.Identity:
        if self.identity is None:
            raise HttpError(401, "Log in first.")
        return self.identity

    def require_user(self) -> tuple[auth.Identity, dict]:
        identity = self.require_identity()
        user = users.get_user(identity.user_id)
        if user is None:
            raise HttpError(403, "Finish creating your account first.")
        return identity, user

    def require_admin(self) -> auth.Identity:
        identity = self.require_identity()
        if not identity.is_admin:
            raise HttpError(403, "Admins only.")
        return identity


ROUTES: list[tuple[str, re.Pattern, object]] = []


def route(method: str, pattern: str):
    regex = re.compile("^" + re.sub(r"\{(\w+)\+\}", r"(?P<\1>.+)", re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)) + "$")

    def register(fn):
        ROUTES.append((method, regex, fn))
        return fn
    return register


def handler(event, _context):
    req = Request(event)
    if req.method == "OPTIONS":
        return _resp(204)
    try:
        path_matched = False
        for method, regex, fn in ROUTES:
            m = regex.match(req.path)
            if not m:
                continue
            path_matched = True
            if method == req.method:
                return fn(req, **m.groupdict())
        return _resp(405 if path_matched else 404, {"message": "Not found."})
    except HttpError as e:
        return _resp(e.status, {"message": str(e)})
    except ValidationError as e:
        return _resp(400, {"message": str(e)})
    except NotFound as e:
        return _resp(404, {"message": str(e)})
    except Forbidden as e:
        return _resp(403, {"message": str(e)})
    except Conflict as e:
        return _resp(409, {"message": str(e)})
    except auth.AuthNotConfigured as e:
        log.error("auth not configured: %s", e)
        return _resp(501, {"message": "Login is not configured on this server."})


# --------------------------------------------------------------------------- auth

@route("POST", "/auth/dev-login")
def dev_login(req: Request):
    if auth.mode() != "dev":
        raise HttpError(404, "Not found.")
    return _resp(200, auth.issue_dev_token(req.json().get("email")))


# --------------------------------------------------------------------------- auctions

@route("GET", "/auctions")
def list_auctions(req: Request):
    category = req.query.get("category") or None
    if category is not None and category not in CATEGORIES:
        raise ValidationError("unknown category")
    return _resp(200, {"auctions": [public_auction(a) for a in repository.list_auctions(category)]})


@route("POST", "/auctions")
def create_auction(req: Request):
    _, user = req.require_user()
    if user.get("sellerStatus") != APPROVED:
        raise HttpError(403, "Your account is not approved for selling yet.")
    data = parse_create_auction(req.json(), user["userId"])
    storage.require_uploaded(data["images"])
    item = new_auction_item(data, user)
    repository.create_auction(item)
    schedules.schedule_close(item["auctionId"], item["endsAt"])
    return _resp(201, {"auction": public_auction(item)})


@route("GET", "/auctions/{auction_id}")
def get_auction(req: Request, auction_id: str):
    snap = repository.snapshot(parse_auction_id({"auctionId": auction_id}))
    return _resp(200, snap) if snap else _resp(404, {"message": "Auction not found."})


@route("PATCH", "/auctions/{auction_id}")
def edit_auction(req: Request, auction_id: str):
    identity = req.require_identity()
    auction_id = parse_auction_id({"auctionId": auction_id})
    body = req.json()
    if "images" in body and isinstance(body["images"], list):
        current = repository.get_auction(auction_id) or {}
        storage.require_uploaded([k for k in body["images"] if k not in (current.get("images") or [])
                                  and isinstance(k, str) and k.startswith(f"uploads/{identity.user_id}/")])
    item, changes = repository.update_auction(auction_id, identity.user_id, body)
    if "endsAt" in changes:
        schedules.schedule_close(auction_id, int(item["endsAt"]))
    return _resp(200, {"auction": public_auction(item)})


@route("POST", "/auctions/{auction_id}/cancel")
def cancel_auction(req: Request, auction_id: str):
    identity = req.require_identity()
    auction_id = parse_auction_id({"auctionId": auction_id})
    item = repository.cancel_auction(auction_id, identity.user_id, is_admin=identity.is_admin)
    schedules.cancel_close(auction_id)
    return _resp(200, {"auction": public_auction(item)})


# --------------------------------------------------------------------------- me

@route("GET", "/me")
def get_me(req: Request):
    identity = req.require_identity()
    user = users.get_user(identity.user_id)
    if user is None:
        # Logged in with the provider, but no account row yet -> onboarding.
        return _resp(404, {"message": "No account yet.", "email": identity.email})
    return _resp(200, {"user": private_user(user, is_admin=identity.is_admin)})


@route("POST", "/me")
def create_me(req: Request):
    identity = req.require_identity()
    data = parse_registration(req.json())
    user = users.create_user(identity, data["displayName"], data["requestSeller"])
    return _resp(201, {"user": private_user(user, is_admin=identity.is_admin)})


@route("PUT", "/me")
def update_me(req: Request):
    identity, _ = req.require_user()
    changes = parse_profile_update(req.json(), identity.user_id)
    if changes.get("avatarKey"):
        storage.require_uploaded([changes["avatarKey"]])
    user = users.update_profile(identity.user_id, changes)
    return _resp(200, {"user": private_user(user, is_admin=identity.is_admin)})


@route("POST", "/me/seller-request")
def seller_request(req: Request):
    identity, _ = req.require_user()
    user = users.request_seller(identity.user_id)
    return _resp(200, {"user": private_user(user, is_admin=identity.is_admin)})


@route("GET", "/me/bids")
def my_bids(req: Request):
    identity = req.require_identity()
    bids = repository.bids_by_bidder(identity.user_id)
    by_auction: dict[str, list[dict]] = {}
    for b in bids:  # newest first
        by_auction.setdefault(b["auctionId"], []).append(public_bid(b))
    auctions = repository.get_auctions(list(by_auction))
    entries = [{"auction": public_auction(auctions[aid]), "bids": bs}
               for aid, bs in by_auction.items() if aid in auctions]
    return _resp(200, {"entries": entries})


@route("GET", "/me/auctions")
def my_auctions(req: Request):
    identity = req.require_identity()
    items = repository.auctions_by_seller(identity.user_id, include_cancelled=True)
    return _resp(200, {"auctions": [public_auction(a) for a in items]})


@route("POST", "/uploads")
def create_upload(req: Request):
    identity, _ = req.require_user()
    body = req.json()
    return _resp(200, storage.create_upload(identity.user_id, body.get("contentType"), body.get("size")))


# --------------------------------------------------------------------------- public users / images

@route("GET", "/users/{user_id}")
def get_user(req: Request, user_id: str):
    user = users.get_user(parse_id(user_id, "userId"))
    if user is None:
        raise NotFound("User not found.")
    return _resp(200, {"user": public_user(user)})


@route("GET", "/users/{user_id}/auctions")
def user_auctions(req: Request, user_id: str):
    items = repository.auctions_by_seller(parse_id(user_id, "userId"), include_cancelled=False)
    return _resp(200, {"auctions": [public_auction(a) for a in items]})


@route("GET", "/images/{key+}")
def get_image(req: Request, key: str):
    if not IMAGE_KEY_RE.match(key):
        raise NotFound("Image not found.")
    return {"statusCode": 302, "headers": {"Location": storage.backend.download_url(key),
                                           "Cache-Control": "private, max-age=600"}, "body": ""}


# --------------------------------------------------------------------------- admin

@route("GET", "/admin/users")
def admin_users(req: Request):
    req.require_admin()
    items = users.all_users() if req.query.get("filter") == "all" else users.pending_users()
    return _resp(200, {"users": [private_user(u, is_admin=u.get("email", "").lower() in auth.admin_emails())
                                 for u in items]})


@route("POST", "/admin/users/{user_id}")
def admin_set_approval(req: Request, user_id: str):
    req.require_admin()
    user = users.set_approval(parse_id(user_id, "userId"), parse_approval(req.json()))
    log.info("approval change for %s: buyer=%s seller=%s", user_id, user.get("buyerStatus"), user.get("sellerStatus"))
    return _resp(200, {"user": private_user(user, is_admin=user.get("email", "").lower() in auth.admin_emails())})
