"""Who is making this request?

Everything in the backend asks this module for an `Identity` and never trusts a
user id sent by the client. The *source* of identities is pluggable via AUTH_MODE:

  dev      Built in. POST /auth/dev-login {email} returns an HMAC-signed token.
           No passwords: anyone can log in as any email. For local work and demos only.
  cognito  NOT IMPLEMENTED YET. Sign-up, login, password reset and email verification
  supabase come from the provider; `_provider_identity` must verify the provider's JWT
           and map it to an Identity (user_id = the token's `sub`, plus `email`).

Account *approval* (buyer / seller) is not the provider's job — it lives on our
Users table (auction/users.py) and is granted by admins, whatever the provider.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass

from .models import ValidationError, now_ms

log = logging.getLogger(__name__)

DEV_TOKEN_TTL_MS = 7 * 24 * 3600 * 1000
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str

    @property
    def is_admin(self) -> bool:
        return self.email.lower() in admin_emails()


class AuthNotConfigured(RuntimeError):
    pass


def mode() -> str:
    return os.environ.get("AUTH_MODE", "dev")


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def bearer_token(headers: dict | None) -> str | None:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    value = headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return None


def identify(token: str | None) -> Identity | None:
    """Returns None for a missing, malformed, forged or expired token."""
    if not token:
        return None
    if mode() == "dev":
        return _dev_identity(token)
    return _provider_identity(token)


# --------------------------------------------------------------------------- dev mode

def _dev_secret() -> bytes:
    secret = os.environ.get("DEV_AUTH_SECRET", "")
    if len(secret) < 16:
        raise AuthNotConfigured("DEV_AUTH_SECRET must be set (at least 16 characters) for AUTH_MODE=dev")
    return secret.encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def dev_user_id(email: str) -> str:
    # Stable per email, so logging in again lands on the same account.
    return "u_" + hashlib.sha256(email.lower().encode()).hexdigest()[:24]


def issue_dev_token(email: str, at_ms: int | None = None) -> dict:
    if mode() != "dev":
        raise AuthNotConfigured("dev login is disabled (AUTH_MODE is not 'dev')")
    if not isinstance(email, str) or not _EMAIL_RE.match(email.strip()):
        raise ValidationError("enter a valid email address")
    email = email.strip().lower()
    expires = (at_ms if at_ms is not None else now_ms()) + DEV_TOKEN_TTL_MS
    payload = _b64(json.dumps({"sub": dev_user_id(email), "email": email, "exp": expires}).encode())
    sig = _b64(hmac.new(_dev_secret(), payload.encode(), hashlib.sha256).digest())
    return {"token": f"{payload}.{sig}", "userId": dev_user_id(email), "expiresAt": expires}


def _dev_identity(token: str) -> Identity | None:
    try:
        payload, sig = token.split(".")
        expected = _b64(hmac.new(_dev_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        claims = json.loads(_unb64(payload))
        if int(claims["exp"]) <= now_ms():
            return None
        return Identity(user_id=claims["sub"], email=claims["email"])
    except (ValueError, KeyError, TypeError):
        return None


# --------------------------------------------------------------------------- real providers

def _provider_identity(token: str) -> Identity | None:
    # Plug Cognito / Supabase in here: verify the JWT signature against the
    # provider's JWKS, check `exp`, `iss` and `aud`, then return
    # Identity(user_id=claims["sub"], email=claims["email"]).
    raise AuthNotConfigured(f"AUTH_MODE={mode()!r} is not implemented yet")
