"""Who is making this request?

Everything in the backend asks this module for an `Identity` and never trusts a
user id sent by the client. The *source* of identities is pluggable via AUTH_MODE:

  dev      Built in. POST /auth/dev-login {email} returns an HMAC-signed token.
           No passwords: anyone can log in as any email. For local work and demos only.
  cognito  Sign-up, login, password reset and email verification happen in an Amazon
           Cognito user pool (the browser talks to Cognito directly). The client sends
           the Cognito *ID token*; we verify its RS256 signature against the pool's
           JWKS plus iss / aud / token_use / exp, and require a verified email.

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
import time
import urllib.request
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


# --------------------------------------------------------------------------- Cognito

# SHA-256 DigestInfo prefix for EMSA-PKCS1-v1_5 (RFC 8017 section 9.2).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_JWKS_TTL_S = 3600
_jwks_cache: dict = {"keys": {}, "fetched": 0.0}
CLOCK_SKEW_S = 60


def _cognito_issuer() -> str:
    pool = os.environ.get("COGNITO_USER_POOL_ID", "")
    if not pool:
        raise AuthNotConfigured("COGNITO_USER_POOL_ID must be set for AUTH_MODE=cognito")
    region = pool.split("_", 1)[0]
    return f"https://cognito-idp.{region}.amazonaws.com/{pool}"


def _fetch_jwks(issuer: str) -> dict:
    with urllib.request.urlopen(f"{issuer}/.well-known/jwks.json", timeout=5) as r:
        return json.loads(r.read())


def _signing_key(kid: str) -> dict | None:
    """Keys are cached for an hour; an unknown kid (key rotation) forces one refetch."""
    now = time.time()
    age = now - _jwks_cache["fetched"]
    # Refetch when stale, or for an unknown kid, but at most every 10 s so tokens
    # with made-up kids can't make us hammer Cognito.
    if age > _JWKS_TTL_S or (kid not in _jwks_cache["keys"] and age > 10):
        _jwks_cache["keys"] = {k["kid"]: k for k in _fetch_jwks(_cognito_issuer()).get("keys", [])}
        _jwks_cache["fetched"] = now
    return _jwks_cache["keys"].get(kid)


def _rsa_sha256_verify(n: int, e: int, message: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 verification. Only public-key operations, so plain
    integer arithmetic is safe here (nothing secret to leak through timing)."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    em = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    digest = _SHA256_PREFIX + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(em, expected)


def _provider_identity(token: str) -> Identity | None:
    if mode() != "cognito":
        raise AuthNotConfigured(f"AUTH_MODE={mode()!r} is not supported")
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    if not client_id:
        raise AuthNotConfigured("COGNITO_CLIENT_ID must be set for AUTH_MODE=cognito")
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_unb64(header_b64))
        if header.get("alg") != "RS256":
            return None
        jwk = _signing_key(str(header.get("kid")))
        if jwk is None or jwk.get("kty") != "RSA":
            return None
        n = int.from_bytes(_unb64(jwk["n"]), "big")
        e = int.from_bytes(_unb64(jwk["e"]), "big")
        if not _rsa_sha256_verify(n, e, f"{header_b64}.{payload_b64}".encode(), _unb64(sig_b64)):
            return None
        claims = json.loads(_unb64(payload_b64))
        now = time.time()
        if (claims.get("iss") != _cognito_issuer() or claims.get("token_use") != "id"
                or claims.get("aud") != client_id or float(claims["exp"]) <= now - CLOCK_SKEW_S
                or float(claims.get("iat", 0)) > now + CLOCK_SKEW_S):
            return None
        # Admin rights hang off the email, so it must be one the user proved they own.
        if claims.get("email_verified") not in (True, "true") or not claims.get("email"):
            return None
        return Identity(user_id=claims["sub"], email=str(claims["email"]).lower())
    except (ValueError, KeyError, TypeError):
        return None
