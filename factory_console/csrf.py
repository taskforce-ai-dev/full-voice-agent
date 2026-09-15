"""Server-signed, owner-bound CSRF tokens for the review-only console."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable


_SCOPE = "factory-review-writes"
_VALID_WRITE_PATHS = frozenset({"/v1/jobs"})
_VALID_ACTIONS = frozenset(
    {
        "inspect",
        "plan",
        "approve-knowledge",
        "approve-plan",
        "generate",
        "verify",
        "open-pr",
    }
)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class HMACCSRFTokenVerifier:
    """Issues a fixed write-scope token; callers cannot choose a signed path."""

    def __init__(self, secret: bytes, *, ttl_seconds: int, now: Callable[[], float] = time.time) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("a 32-byte CSRF signing secret is required")
        if not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 900:
            raise ValueError("CSRF token lifetime must be between 60 and 900 seconds")
        self._secret = secret
        self._ttl_seconds = ttl_seconds
        self._now = now

    def issue(self, *, owner_identity: str) -> str:
        if not isinstance(owner_identity, str) or not owner_identity:
            raise ValueError("owner identity is required")
        issued_at = int(self._now())
        payload = json.dumps(
            {
                "exp": issued_at + self._ttl_seconds,
                "iat": issued_at,
                "nonce": secrets.token_urlsafe(16),
                "scope": _SCOPE,
                "sub": owner_identity,
                "v": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded = _encode(payload)
        signature = _encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(self, *, token: str, owner_identity: str, method: str, path: str) -> bool:
        if not isinstance(token, str) or len(token) > 2048 or token.count(".") != 1:
            return False
        encoded, signature = token.split(".")
        expected = _encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return False
        try:
            payload = json.loads(_decode(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        issued_at, expires_at = payload.get("iat"), payload.get("exp")
        if (
            payload.get("v") != 1
            or payload.get("scope") != _SCOPE
            or not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at - issued_at != self._ttl_seconds
            or not isinstance(payload.get("sub"), str)
            or not hmac.compare_digest(payload["sub"], owner_identity)
        ):
            return False
        now = int(self._now())
        return issued_at <= now < expires_at and self._is_review_write(method, path)

    @staticmethod
    def _is_review_write(method: str, path: str) -> bool:
        if method != "POST":
            return False
        if path in _VALID_WRITE_PATHS:
            return True
        parts = path.split("/")
        return len(parts) == 5 and parts[:3] == ["", "v1", "jobs"] and parts[4] in _VALID_ACTIONS
